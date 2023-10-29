import torch
from torch import nn
from vit_pytorch.sinkhorn import SinkhornDistance
from vit_pytorch.swd import SWD5, SWD6, SWD7
from einops import rearrange, repeat
from einops.layers.torch import Rearrange

# helpers

def pair(t):
    return t if isinstance(t, tuple) else (t, t)

# classes

class PreNorm(nn.Module):
    def __init__(self, dim, fn):
        super().__init__()
        self.norm = nn.LayerNorm(dim)
        self.fn = fn
    def forward(self, x, **kwargs):
        return self.fn(self.norm(x), **kwargs)

class FeedForward(nn.Module):
    def __init__(self, dim, hidden_dim, dropout = 0.):
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(dim, hidden_dim),
            nn.GELU(),
            nn.Dropout(dropout),
            nn.Linear(hidden_dim, dim),
            nn.Dropout(dropout)
        )
    def forward(self, x):
        return self.net(x)

class Attention(nn.Module):
    def __init__(self, dim, heads = 8, dim_head = 64, dropout = 0., max_iter=3, eps=1, attn='trans'):
        super().__init__()
        inner_dim = dim_head *  heads
        project_out = not (heads == 1 and dim_head == dim)

        self.heads = heads
        self.scale = dim_head ** -0.5
        self.max_iter = max_iter
        self.swd = SWD7()
        self.sink = SinkhornDistance(eps=eps, max_iter=max_iter)
        self.attend = nn.Softmax(dim = -1)
        self.dropout = nn.Dropout(dropout)
        self.to_qkv = nn.Linear(dim, inner_dim * 3, bias = False)

        self.to_out = nn.Sequential(
            nn.Linear(inner_dim, dim),
            nn.Dropout(dropout)
        ) if project_out else nn.Identity()

        self.attn = attn

    def forward(self, x, stage='train'):
        # with torch.no_grad():
        #     self.to_qkv.weight.div_(torch.norm(self.to_qkv.weight, dim=1, keepdim=True))
        qkv = self.to_qkv(x).chunk(3, dim = -1)
        q, k, v = map(lambda t: rearrange(t, 'b n (h d) -> b h n d', h = self.heads), qkv)

        if self.attn == 'trans':
            dots = torch.matmul(q, k.transpose(-1, -2)) * self.scale
            attn = self.attend(dots)
            attn = self.dropout(attn)
            out = torch.matmul(attn, v)
        elif self.attn == 'sink':
            dots = torch.matmul(q, k.transpose(-1, -2)) * self.scale
            dots_former_shape = dots.shape
            dots = dots.view(-1, dots_former_shape[2], dots_former_shape[3])
            attn = self.sink(dots)[0]
            attn = attn * attn.shape[-1]
            attn = attn.view(dots_former_shape)
            out = torch.matmul(attn, v)
        elif self.attn == 'swd':
            out, attn = self.swd(q, k, v, stage)

        out = rearrange(out, 'b h n d -> b n (h d)')
        return self.to_out(out), attn

class Transformer(nn.Module):
    def __init__(self, dim, depth, heads, dim_head, mlp_dim, dropout = 0.1, max_iter=1, eps=1, attn='trans'):
        super().__init__()
        self.layers = nn.ModuleList([])
        for _ in range(depth):
            self.layers.append(nn.ModuleList([
                PreNorm(dim, Attention(dim, heads = heads, dim_head = dim_head, dropout = dropout, max_iter=max_iter, eps=eps, attn=attn)),
                PreNorm(dim, FeedForward(dim, mlp_dim, dropout = dropout))
            ]))
    def forward(self, x, stage='train'):
        attn_weights = None
        for idx, (attn, ff) in enumerate(self.layers):
            if idx == 0:
                attn_x, attn_matrix = attn(x, stage=stage)
                x_sorted, x_indices = x.sort(dim=-2, descending=True)
                x = attn_x + x_sorted
            else:
                attn_x, attn_matrix = attn(x, stage=stage)
                x = attn_x + x
            x = ff(x) + x
            # attn_weights.append(attn_matrix.cpu().detach().numpy())
            if attn_weights is None and attn_matrix is not None:
                attn_weights = attn_matrix.detach().clone()
        return x, attn_weights


class Transformer_only_Att(nn.Module):
    def __init__(self, dim, depth, heads, dim_head, mlp_dim, dropout = 0., max_iter=1, eps=1):
        super().__init__()
        self.layers = nn.ModuleList([])
        for _ in range(depth):
            self.layers.append(nn.ModuleList([
                PreNorm(dim, Attention(dim, heads = heads, dim_head = dim_head, dropout = dropout, max_iter=max_iter, eps=eps)),
                PreNorm(dim, FeedForward(dim, mlp_dim, dropout = dropout))
            ]))
    def forward(self, x, stage='train'):
        attn_weights = None
        # for attn, ff in self.layers:
        #     attn_x, attn_matrix = attn(x, stage=stage)
        #     x = attn_x + x
        #     if attn_weights is None and attn_matrix is not None:
        #         attn_weights = attn_matrix.detach().clone()

        return x, attn_weights

class ViT(nn.Module):
    def __init__(self, *, image_size, patch_size, num_classes, dim, depth, heads, mlp_dim, pool = 'cls', channels = 3,
                 dim_head = 64, dropout = 0., emb_dropout = 0., max_iter=1, eps=1, attn='trans'):
        super().__init__()
        image_height, image_width = pair(image_size)
        patch_height, patch_width = pair(patch_size)

        assert image_height % patch_height == 0 and image_width % patch_width == 0, 'Image dimensions must be divisible by the patch size.'

        num_patches = (image_height // patch_height) * (image_width // patch_width)
        patch_dim = channels * patch_height * patch_width
        assert pool in {'cls', 'mean'}, 'pool type must be either cls (cls token) or mean (mean pooling)'

        self.to_patch_embedding = nn.Sequential(
            Rearrange('b c (h p1) (w p2) -> b (h w) (p1 p2 c)', p1 = patch_height, p2 = patch_width),
            nn.Linear(patch_dim, dim),
        )

        self.pos_embedding = nn.Parameter(torch.randn(1, num_patches + 1, dim))
        self.cls_token = nn.Parameter(torch.randn(1, 1, dim))
        self.dropout = nn.Dropout(emb_dropout)

        self.transformer = Transformer(dim, depth, heads, dim_head, mlp_dim, dropout, max_iter=max_iter, eps=eps, attn=attn)

        self.pool = pool
        self.to_latent = nn.Identity()

        self.mlp_head = nn.Sequential(
            nn.LayerNorm(dim),
            nn.Linear(dim, num_classes)
        )

    def forward(self, img, stage='train'):
        x = self.to_patch_embedding(img)
        b, n, _ = x.shape

        cls_tokens = repeat(self.cls_token, '() n d -> b n d', b = b)
        x = torch.cat((cls_tokens, x), dim=1)
        x += self.pos_embedding[:, :(n + 1)]
        x = self.dropout(x)

        # indices_rand = torch.randperm(x.size(dim=-2) - 1)
        # x_0 = x[:, 0, :].unsqueeze(-2)
        # x_b = x[:, 1:, :][:, indices_rand, :]
        # x = torch.cat([x_0, x_b], dim=-2)

        trans_x, attn_weights = self.transformer(x, stage='train')
        x = trans_x

        x = x.mean(dim = 1) if self.pool == 'mean' else x[:, 0]

        x = self.to_latent(x)
        return self.mlp_head(x), attn_weights


class ViT_only_Att(nn.Module):
    def __init__(self, *, image_size, patch_size, num_classes, dim, depth, heads, mlp_dim, pool = 'cls', channels = 3,
                 dim_head = 64, dropout = 0., emb_dropout = 0., max_iter=1, eps=1):
        super().__init__()
        image_height, image_width = pair(image_size)
        patch_height, patch_width = pair(patch_size)

        assert image_height % patch_height == 0 and image_width % patch_width == 0, 'Image dimensions must be divisible by the patch size.'

        num_patches = (image_height // patch_height) * (image_width // patch_width)
        patch_dim = channels * patch_height * patch_width
        assert pool in {'cls', 'mean'}, 'pool type must be either cls (cls token) or mean (mean pooling)'

        self.to_patch_embedding = nn.Sequential(
            Rearrange('b c (h p1) (w p2) -> b (h w) (p1 p2 c)', p1 = patch_height, p2 = patch_width),
            nn.Linear(patch_dim, dim),
        )

        self.pos_embedding = nn.Parameter(torch.randn(1, num_patches + 1, dim))
        self.cls_token = nn.Parameter(torch.randn(1, 1, dim))
        self.dropout = nn.Dropout(emb_dropout)

        self.transformer = Transformer_only_Att(dim, depth, heads, dim_head, mlp_dim, dropout, max_iter=max_iter, eps=eps)

        self.pool = pool
        self.to_latent = nn.Identity()

        self.mlp_head = nn.Sequential(
            nn.LayerNorm(dim),
            nn.Linear(dim, num_classes)
        )

    def forward(self, img, stage='train'):
        x = self.to_patch_embedding(img)
        # b, n, _ = x.shape
        #
        # cls_tokens = repeat(self.cls_token, '() n d -> b n d', b = b)
        # x = torch.cat((cls_tokens, x), dim=1)
        # x += self.pos_embedding[:, :(n + 1)]
        # x = self.dropout(x)
        # trans_x, attn_weights = self.transformer(x, stage)
        # x = trans_x
        #
        # x = x.mean(dim = 1) if self.pool == 'mean' else x[:, 0]
        #
        # x = self.to_latent(x)
        return self.mlp_head(x), attn_weights