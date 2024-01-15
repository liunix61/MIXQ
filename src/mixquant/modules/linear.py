 
import torch
import torch.nn as nn
 
import mixlib

 
 
from EETQ import quant_weights, preprocess_weights, w8_a16_gemm






class MixLinear_GEMM(nn.Module):
    def __init__(self, in_features, out_features, bias, dev, weight_only = False, cache = None):
        super().__init__()
        
 
        self.in_features = in_features
        self.out_features = out_features


        # self.register_buffer('qweight', torch.empty((out_features,in_features), dtype=torch.int8, device=dev))
        # self.register_buffer('scale', torch.empty((1,1), dtype=torch.float16, device=dev))
        if weight_only is False:
            self.register_buffer('q_weight', torch.empty((out_features,in_features), dtype=torch.int8, device=dev,requires_grad=False))
            self.register_buffer('scale_col', torch.empty((1,out_features), dtype=torch.float16, device=dev,requires_grad=False))
        else:
            self.register_buffer('q_weight', torch.empty((in_features,out_features), dtype=torch.int8, device=dev,requires_grad=False))
            self.register_buffer('scale_col', torch.empty((out_features), dtype=torch.float16, device=dev,requires_grad=False))

        if bias:

            self.register_buffer('bias', torch.empty((out_features), dtype=torch.float16, device=dev,requires_grad=False))
        else:
            self.bias = None
        self.cnt = 0
        self.forward_without_precondition_len = -1
        self.quant = True
        self.cache = cache
        self.weight_only = weight_only
        self.ind = None
        self.add_outliers = True

        if cache is not None:
            self.sigma = torch.ones((1, 1),dtype=torch.float16, requires_grad=False,
                                            device = cache.sigma.device)
            self.sigma[0] = cache.sigma
        self.weight_cache = None

    @classmethod
    def from_linear(cls, linear, weight_only=False, init_only=False,cache=None):

        quant_linear = cls(linear.in_features, linear.out_features, linear.bias is not None, 
                           linear.weight.device,weight_only,cache)
        if init_only is True: 
            return quant_linear   
             
        if weight_only is True:
            int8_weight_cpu = torch.t(linear.weight.data).contiguous().cpu()
            int8_weight, scales = quant_weights(int8_weight_cpu, torch.int8, False)
            quant_linear.q_weight.copy_ (int8_weight)
            quant_linear.scale_col.copy_(scales.half())

        else:
            scale =   (torch.max(torch.abs(linear.weight.data), dim=1)[0].unsqueeze(1) / (
                            1 << (8 - 1) - 1)).to(torch.float16).reshape((1,linear.out_features))
            

            #self.scale_col = self.scale.repeat((1,self.N))
            quant_linear.scale_col.copy_(scale)
            tmp = linear.weight.data.cuda()
            tmp /= quant_linear.scale_col.T
            tmp = tmp.round().to(torch.int8)
            quant_linear.q_weight.copy_(tmp)            


        if linear.bias is not None:
            quant_linear.bias.copy_(linear.bias.half())

        return quant_linear
    
    @torch.no_grad()
    def quant_weight(self,cache,layer=None,weight_only=False):

        self.K = self.weight.shape[1]

        self.N = self.weight.shape[0]
        self.layer = layer 
        self.quant = True
        self.cache = cache
        self.ind = None
        self.weight_cache = None
        self.add_outliers = True
        self.cnt = 0
        self.weight_only = weight_only

        if self.bias  is not None:
            raise "no implement error"
        

        
        self.register_buffer("sigma",
                            torch.empty((1, 1),
                                        dtype=torch.float16, requires_grad=False,
                                        device= cache.device))
        if weight_only is True:

            int8_weight_cpu = torch.t(self.weight).contiguous().cpu()
            int8_weight, scales = quant_weights(int8_weight_cpu, torch.int8, False)

            self.q_weight.copy_ (int8_weight)

            self.scale_col.copy_(scales.half())

        else:

            self.register_buffer('scale_col', torch.empty((1,self.N), dtype=torch.float16, device=cache.device))

        

            scale =   (torch.max(torch.abs(self.weight), dim=1)[0].unsqueeze(1) / (
                            1 << (8 - 1) - 1)).to(torch.float16).reshape((1,self.N))
            #self.scale_col = self.scale.repeat((1,self.N))
            self.scale_col.copy_(scale)
            tmp = self.weight.cuda()
            tmp /= self.scale_col.T
            tmp = tmp.round().to(torch.int8)
            self.q_weight.copy_(tmp)
            tmp = tmp.to('cpu')
            del tmp



        self.SetSigma(cache.sigma)

 

        self.forward_without_precondition_len = -1


        del self.weight
        torch.cuda.empty_cache()
    @torch.no_grad()
    def SetSigma(self,sigma):
        self.sigma[0] = sigma
         

    
    @torch.no_grad()
    def FindOutliers(self,Activation):

        
        tmp = torch.unique(torch.where((  Activation.abs() > self.sigma ))[1])
        return tmp.to(torch.int32)
    @torch.no_grad()
    def ExtractFP16weight(self):
        assert self.ind is not None
        assert self.weight_cache is  None
        self.weight_cache = self.q_weight[:,self.ind].to(torch.float16)
        self.weight_cache *=  self.scale_col.T
        self.q_weight[:,self.ind] *= 0
        return self.weight_cache

    @torch.no_grad()
    def forward(self, x, cache = None, weight_only = False):
        #memory = torch.cuda.memory_allocated()/1024/1024
        #print("start forward",memory)
        #print(self.weight_only)

        #torch.cuda.set_stream(cache.stream)
        if cache is  None:
            cache = self.cache

 
        cache.shape = x.shape[:-1] + (self.out_features, )

 
        inputs = x.reshape(-1, x.shape[-1])
 
        M =  inputs.shape[0]
        

        if self.weight_only is True:

            y =   w8_a16_gemm(inputs, self.q_weight, self.scale_col)

            if self.bias is not None:
                y += self.bias
            return y

       
        if self.ind is None:
            self.ind = self.FindOutliers(inputs)
            cache.ind = self.ind
            self.weight_cache = self.ExtractFP16weight()  
        #print("after get ind",torch.cuda.memory_allocated()/1024/1024 - memory)

        if len(self.ind):
             
            cache.activation_outliers = mixlib.ExtractOutliersAndSetToZeros(self.ind,inputs)

            
            outliers_fp16 = torch.mm( cache.activation_outliers ,  self.weight_cache.T)
 
            
        else:
            outliers_fp16 =  None

        #print("after compute outliers",torch.cuda.memory_allocated()/1024/1024 - memory)
        cache.x_scale = torch.amax(inputs.abs(),dim=1) / 127.0


        #print("after compute x scale",torch.cuda.memory_allocated()/1024/1024 - memory)
        if self.add_outliers:
            if cache.x_scale.max() > self.sigma / 127.0:
                 
                ind = torch.unique(torch.where((  inputs.abs() > self.sigma ))[1])
                ind = ind.to(torch.int32)
                activation_outliers = mixlib.ExtractOutliersAndSetToZeros(ind,inputs)
   
                weight_cache = self.q_weight[:,ind].to(torch.float16) *  self.scale_col.T
                if outliers_fp16 is None:
                    outliers_fp16 = torch.mm( activation_outliers ,weight_cache.T  )
                else:
                    outliers_fp16 = outliers_fp16 + torch.mm( activation_outliers ,weight_cache.T  )
                self.q_weight[:,ind] *= 0
                if len(self.ind) == 0:
                    cache.activation_outliers = activation_outliers
                else:
                    cache.activation_outliers =  torch.hstack((cache.activation_outliers,activation_outliers))
                self.weight_cache =  torch.hstack((self.weight_cache,weight_cache))
                self.ind = torch.hstack((self.ind,ind))
                cache.ind = self.ind

    
                cache.x_scale = torch.amax(inputs.abs(),dim=1) / 127.0
 
            self.cnt += 1
            if self.cnt >= 10 or len(self.ind) > 256:
                self.add_outliers = False
            torch.cuda.empty_cache()
            #print("after add outliers",torch.cuda.memory_allocated()/1024/1024 - memory)

 


        cache.q_xcache = mixlib.Int8quantize(inputs,cache.x_scale)

        if outliers_fp16 is None:

            y1 = mixlib.int8FusedDequantize(cache.q_xcache, 
                                            self.q_weight, 
                                            cache.x_scale,
                                            self.scale_col,
                                            self.cache.zeros,
                                            M,self.out_features,
                                            self.in_features)    
        else:
            y1 = mixlib.int8FusedDequantize(cache.q_xcache, 
                                            self.q_weight, 
                                            cache.x_scale,
                                            self.scale_col,
                                            outliers_fp16,
                                            M,self.out_features,
                                            self.in_features)

        if self.bias is not None:
            y1 += self.bias
        #print("after gemm",torch.cuda.memory_allocated()/1024/1024 - memory)
        return y1.reshape(cache.shape) 
    

    @torch.no_grad()
    def forward_without_precondition(self, x, cache):
        #memory = torch.cuda.memory_allocated()/1024/1024
        #print("start forward",memory)        
 
        inputs = x.reshape(-1, x.shape[-1])
        M =  inputs.shape[0]
        assert M == cache.shape[0]

        if not self.forward_without_precondition_len == len(cache.ind):
            self.ind = cache.ind
            self.weight_cache = self.q_weight[:,self.ind].to(torch.float16) *  self.scale_col.T
            self.forward_without_precondition_len = len(self.ind)

            #print("after weight_cache",torch.cuda.memory_allocated()/1024/1024 - memory)

        if len(self.ind):
             

            outliers_fp16 = torch.mm( cache.activation_outliers ,  self.weight_cache.T)
            y1 = mixlib.int8FusedDequantize(cache.q_xcache, 
                                                    self.q_weight, 
                                                    cache.x_scale,
                                                    self.scale_col,
                                                    outliers_fp16,
                                                    M,self.out_features,self.in_features)  
            
        else:

            y1 = mixlib.int8FusedDequantize(cache.q_xcache, 
                                                    self.q_weight, 
                                                    cache.x_scale,
                                                    self.scale_col,
                                                    self.cache.zeros,
                                                    M,self.out_features,self.in_features)  
        #print("after gemm",torch.cuda.memory_allocated()/1024/1024 - memory)


        if self.bias is not None:
            y1 += self.bias
        return y1.reshape(cache.shape)
    @torch.no_grad()
    def forward_without_preconditionFusedSilu(self, x, cache):
        
   
        inputs = x.reshape(-1, x.shape[-1])
        M =  inputs.shape[0]
        assert M == cache.shape[0]


        if not self.forward_without_precondition_len == len(cache.ind):
            self.ind = cache.ind
            self.weight_cache = self.q_weight[:,self.ind].to(torch.float16) 
            self.weight_cache *=  self.scale_col.T
            self.forward_without_precondition_len = len(self.ind)
        if len(self.ind):
             

            outliers_fp16 = torch.mm( cache.activation_outliers,  self.weight_cache.T)
        
            y1 = mixlib.int8FusedDequantizeSilu(cache.q_xcache, 
                                                    self.q_weight, 
                                                    cache.x_scale,
                                                    self.scale_col,
                                                    outliers_fp16,
                                                    M,self.out_features,self.in_features)  
            
        else:

            y1 = mixlib.int8FusedDequantizeSilu(cache.q_xcache, 
                                                    self.q_weight, 
                                                    cache.x_scale,
                                                    self.scale_col,
                                                    self.cache.zeros,
                                                    M,self.out_features,self.in_features)  


        #print("after gemm",torch.cuda.memory_allocated()/1024/1024 - memory)
        if self.bias is not None:
            y1 += self.bias
        return y1.reshape(cache.shape)
    
