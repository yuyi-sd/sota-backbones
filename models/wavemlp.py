import torch
from torch import nn, Tensor
from torch.nn import functional as F
from .layers import DropPath, trunc_normal_


class MLP(nn.Module):
    def __init__(self, dim, hidden_dim, out_dim=None) -> None:
        super().__init__()
        out_dim = out_dim or dim
        self.fc1 = nn.Conv2d(dim, hidden_dim, 1)
        self.act = nn.GELU()
        self.fc2 = nn.Conv2d(hidden_dim, out_dim, 1)

    def forward(self, x: Tensor) -> Tensor:
        return self.fc2(self.act(self.fc1(x)))


class PATM(nn.Module):
    def __init__(self, dim):
        super().__init__()
        self.fc_h = nn.Conv2d(dim, dim, 1, bias=False)
        self.fc_w = nn.Conv2d(dim, dim, 1, bias=False)
        self.fc_c = nn.Conv2d(dim, dim, 1, bias=False)

        self.tfc_h = nn.Conv2d(2*dim, dim, (1, 7), 1, (0, 7//2), groups=dim, bias=False)
        self.tfc_w = nn.Conv2d(2*dim, dim, (7, 1), 1, (7//2, 0), groups=dim, bias=False)
        self.reweight = MLP(dim, dim//4, dim*3)

        self.proj = nn.Conv2d(dim, dim, 1)

        self.theta_h_conv = nn.Sequential(
            nn.Conv2d(dim, dim, 1),
            nn.BatchNorm2d(dim),
            nn.ReLU()
        )
        self.theta_w_conv = nn.Sequential(
            nn.Conv2d(dim, dim, 1),
            nn.BatchNorm2d(dim),
            nn.ReLU()
        )

    def forward(self, x: Tensor) -> Tensor:
        B, C, H, W = x.shape

        theta_h = self.theta_h_conv(x)
        theta_w = self.theta_w_conv(x)

        x_h = self.fc_h(x)
        x_w = self.fc_w(x)
        c = self.fc_c(x)

        x_h = torch.cat([x_h * torch.cos(theta_h), x_h * torch.sin(theta_h)], dim=1)
        x_w = torch.cat([x_w * torch.cos(theta_w), x_w * torch.sin(theta_w)], dim=1)

        h = self.tfc_h(x_h)
        w = self.tfc_w(x_w)

        a = F.adaptive_avg_pool2d(h + w + c, output_size=1)
        a = self.reweight(a).reshape(B, C, 3).permute(2, 0, 1).softmax(dim=0).unsqueeze(-1).unsqueeze(-1)
        x = h * a[0] + w * a[1] + c * a[2]

        x = self.proj(x)
        return x

    
class Block(nn.Module):
    def __init__(self, dim, mlp_ratio=4, dpr=0., norm_layer=nn.BatchNorm2d, use_norm=True):
        super().__init__()
        self.norm1 = norm_layer(dim) if use_norm else nn.Identity()
        self.attn = PATM(dim)
        self.drop_path = DropPath(dpr) if dpr > 0. else nn.Identity()
        self.norm2 = norm_layer(dim) if use_norm else nn.Identity()
        self.mlp = MLP(dim, int(dim*mlp_ratio))

    def forward(self, x: Tensor) -> Tensor:
        x = x + self.drop_path(self.attn(self.norm1(x))) 
        x = x + self.drop_path(self.mlp(self.norm2(x)))
        return x


class PatchEmbedOverlap(nn.Module):
    """Image to Patch Embedding with overlapping
    """
    def __init__(self, patch_size=16, stride=16, padding=0, embed_dim=768, norm_layer=nn.BatchNorm2d, use_norm=True):
        super().__init__()
        self.proj = nn.Conv2d(3, embed_dim, patch_size, stride, padding)
        self.norm = norm_layer(embed_dim) if use_norm else nn.Identity()

    def forward(self, x: torch.Tensor) -> Tensor:
        return self.norm(self.proj(x))


class Downsample(nn.Module):
    """Downsample transition stage"""
    def __init__(self, c1, c2, norm_layer=nn.BatchNorm2d, use_norm=True):
        super().__init__()
        self.proj = nn.Conv2d(c1, c2, 3, 2, 1)
        self.norm = norm_layer(c2) if use_norm else nn.Identity()

    def forward(self, x: Tensor) -> Tensor:
        return self.norm(self.proj(x))


def GroupNorm(dim):
    return nn.GroupNorm(1, dim)


wavemlp_settings = {
    'T': [[2, 2, 4, 2], [4, 4, 4, 4]],       # [layers]
    'S': [[2, 3, 10, 3], [4, 4, 4, 4]],
    'M': [[3, 4, 18, 3], [8, 8, 4, 4]]
}


class WaveMLP(nn.Module):     
    def __init__(self, model_name: str = 'T', pretrained: str = None, num_classes: int = 1000, *args, **kwargs) -> None:
        super().__init__()
        assert model_name in wavemlp_settings.keys(), f"WaveMLP model name should be in {list(wavemlp_settings.keys())}"
        layers, mlp_ratios = wavemlp_settings[model_name]
        drop_path_rate = 0.
        embed_dims = [64, 128, 320, 512]
        norm_layer = nn.BatchNorm2d if model_name == 'T' else GroupNorm
        use_norm = False if model_name == 'M' else True
    
        self.patch_embed = PatchEmbedOverlap(7, 4, 2, embed_dims[0], norm_layer, use_norm)

        network = []

        for i in range(len(layers)):
            stage = nn.Sequential(*[
                Block(embed_dims[i], mlp_ratios[i], drop_path_rate * (j + sum(layers[:i]) / (sum(layers)-1)), norm_layer, use_norm)
            for j in range(layers[i])])
            
            network.append(stage)
            if i >= len(layers) - 1: break
            network.append(Downsample(embed_dims[i], embed_dims[i+1], norm_layer, use_norm))

        self.network = nn.ModuleList(network)
        self.norm = norm_layer(embed_dims[-1])
        self.head = nn.Linear(embed_dims[-1], num_classes)

        # use as a backbone
        self.out_indices = [0, 2, 4, 6]
        # for i, layer in enumerate(self.out_indices):
        #     self.add_module(f"norm{layer}", norm_layer(embed_dims[i]))

        self._init_weights(pretrained)

    def _init_weights(self, pretrained: str = None) -> None:
        if pretrained:
            self.load_state_dict(torch.load(pretrained, map_location='cpu'))
        else:
            for n, m in self.named_modules():
                if isinstance(m, nn.Linear):
                    trunc_normal_(m.weight, std=.02)
                    if m.bias is not None:
                        nn.init.constant_(m.bias, 0)
                elif isinstance(m, (nn.LayerNorm, nn.BatchNorm2d)):
                    nn.init.ones_(m.weight)
                    nn.init.zeros_(m.bias)
                elif isinstance(m, nn.Conv2d):
                    nn.init.xavier_uniform_(m.weight)
                    if m.bias is not None:
                        nn.init.zeros_(m.bias)
                
    def return_features(self, x):
        x = self.patch_embed(x)
        outs = []

        for i, blk in enumerate(self.network):
            print(i)
            x = blk(x)
            if i in self.out_indices:
                out = getattr(self, f"norm{i}")(x)
                outs.append(out)
        return outs
        
    def forward(self, x: torch.Tensor):
        x = self.patch_embed(x)          

        for blk in self.network:
            x = blk(x)

        x = self.norm(x)
        x = self.head(F.adaptive_avg_pool2d(x, output_size=1).flatten(1))
        return x

if __name__ == '__main__':
    model = WaveMLP('M')
    # model._init_weights('C:\\Users\\sithu\\Documents\\weights\\backbones\\wavemlp\\WaveMLP_M.pth')
    x = torch.randn(1, 3, 224, 224)
    y = model(x)
    print(y.shape)