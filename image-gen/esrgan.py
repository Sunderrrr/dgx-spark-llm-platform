"""Real-ESRGAN (RRDBNet) — pure PyTorch inference super-resolution.

We vendor the RRDBNet architecture directly rather than installing `basicsr`:
that package is incompatible with torch 2.10 (torchvision functional_tensor
removed), while the architecture itself is ~150 lines with no dependency.
The RealESRGAN_x4plus.pth (x4) weights are mounted read-only (/esrgan).
"""
import threading

import torch
from torch import nn
from torch.nn import functional as F


def _pixel_unshuffle(x, scale):
    b, c, hh, hw = x.size()
    out_channel = c * (scale * scale)
    h, w = hh // scale, hw // scale
    x_view = x.view(b, c, h, scale, w, scale)
    return x_view.permute(0, 1, 3, 5, 2, 4).reshape(b, out_channel, h, w)


class _ResidualDenseBlock(nn.Module):
    def __init__(self, num_feat=64, num_grow_ch=32):
        super().__init__()
        self.conv1 = nn.Conv2d(num_feat, num_grow_ch, 3, 1, 1)
        self.conv2 = nn.Conv2d(num_feat + num_grow_ch, num_grow_ch, 3, 1, 1)
        self.conv3 = nn.Conv2d(num_feat + 2 * num_grow_ch, num_grow_ch, 3, 1, 1)
        self.conv4 = nn.Conv2d(num_feat + 3 * num_grow_ch, num_grow_ch, 3, 1, 1)
        self.conv5 = nn.Conv2d(num_feat + 4 * num_grow_ch, num_feat, 3, 1, 1)
        self.lrelu = nn.LeakyReLU(negative_slope=0.2, inplace=True)

    def forward(self, x):
        x1 = self.lrelu(self.conv1(x))
        x2 = self.lrelu(self.conv2(torch.cat((x, x1), 1)))
        x3 = self.lrelu(self.conv3(torch.cat((x, x1, x2), 1)))
        x4 = self.lrelu(self.conv4(torch.cat((x, x1, x2, x3), 1)))
        x5 = self.conv5(torch.cat((x, x1, x2, x3, x4), 1))
        return x5 * 0.2 + x


class _RRDB(nn.Module):
    def __init__(self, num_feat, num_grow_ch=32):
        super().__init__()
        self.rdb1 = _ResidualDenseBlock(num_feat, num_grow_ch)
        self.rdb2 = _ResidualDenseBlock(num_feat, num_grow_ch)
        self.rdb3 = _ResidualDenseBlock(num_feat, num_grow_ch)

    def forward(self, x):
        out = self.rdb1(x)
        out = self.rdb2(out)
        out = self.rdb3(out)
        return out * 0.2 + x


class RRDBNet(nn.Module):
    def __init__(self, num_in_ch=3, num_out_ch=3, scale=4, num_feat=64,
                 num_block=23, num_grow_ch=32):
        super().__init__()
        self.scale = scale
        if scale == 2:
            num_in_ch = num_in_ch * 4
        elif scale == 1:
            num_in_ch = num_in_ch * 16
        self.conv_first = nn.Conv2d(num_in_ch, num_feat, 3, 1, 1)
        self.body = nn.Sequential(
            *[_RRDB(num_feat=num_feat, num_grow_ch=num_grow_ch) for _ in range(num_block)])
        self.conv_body = nn.Conv2d(num_feat, num_feat, 3, 1, 1)
        self.conv_up1 = nn.Conv2d(num_feat, num_feat, 3, 1, 1)
        self.conv_up2 = nn.Conv2d(num_feat, num_feat, 3, 1, 1)
        self.conv_hr = nn.Conv2d(num_feat, num_feat, 3, 1, 1)
        self.conv_last = nn.Conv2d(num_feat, num_out_ch, 3, 1, 1)
        self.lrelu = nn.LeakyReLU(negative_slope=0.2, inplace=True)

    def forward(self, x):
        if self.scale == 2:
            feat = _pixel_unshuffle(x, scale=2)
        elif self.scale == 1:
            feat = _pixel_unshuffle(x, scale=4)
        else:
            feat = x
        feat = self.conv_first(feat)
        body_feat = self.conv_body(self.body(feat))
        feat = feat + body_feat
        feat = self.lrelu(self.conv_up1(F.interpolate(feat, scale_factor=2, mode="nearest")))
        feat = self.lrelu(self.conv_up2(F.interpolate(feat, scale_factor=2, mode="nearest")))
        return self.conv_last(self.lrelu(self.conv_hr(feat)))


def load_esrgan(path, device):
    """Load RealESRGAN_x4plus.pth and return the network in eval mode on `device`."""
    net = RRDBNet(num_in_ch=3, num_out_ch=3, scale=4, num_feat=64,
                  num_block=23, num_grow_ch=32)
    state = torch.load(path, map_location="cpu")
    # The Real-ESRGAN checkpoint stores params_ema (and params); we take the EMA.
    if "params_ema" in state:
        state = state["params_ema"]
    elif "params" in state:
        state = state["params"]
    net.load_state_dict(state, strict=True)
    net.to(device).eval()
    return net


def upscale_4x(net, pil_image, tile=256, tile_pad=32):
    """x4 super-resolution of a PIL RGB image, TILE BY TILE.

    A full-resolution pass would blow up the GB10's unified memory (the x4
    activations on 6144x3456 weighed ~6-8 GB and froze the box). So we cut the
    image into `tile` px tiles with a `tile_pad` overlap (network receptive
    field, to avoid seams), upscale each tile, crop its center and glue back.
    Peak memory bounded at ~0.5 GB (tile 320x320 -> 1280x1280), independent of
    the final size.
    """
    import numpy as np
    from PIL import Image as _PILImage

    device = next(net.parameters()).device
    img = np.asarray(pil_image.convert("RGB"), dtype="float32")  # H, W, 3
    h, w = img.shape[:2]
    oh, ow = h * 4, w * 4
    out = np.empty((oh, ow, 3), dtype=np.uint8)
    for ty in range(0, h, tile):
        for tx in range(0, w, tile):
            y0 = max(0, ty - tile_pad)
            x0 = max(0, tx - tile_pad)
            y1 = min(h, ty + tile + tile_pad)
            x1 = min(w, tx + tile + tile_pad)
            patch = img[y0:y1, x0:x1]
            t = torch.from_numpy(patch).permute(2, 0, 1).unsqueeze(0).div(255.0).to(device)
            with torch.inference_mode():
                o = net(t)
            o = o.clamp(0, 1).squeeze(0).permute(1, 2, 0).mul(255.0).round().byte().cpu().numpy()
            # Keep only the center of the tile (the overlap feeds the network's
            # context but is not kept), handling the edges.
            cy0 = (ty - y0) * 4
            cx0 = (tx - x0) * 4
            oy0, ox0 = ty * 4, tx * 4
            hh = min(tile * 4, oh - oy0)
            ww = min(tile * 4, ow - ox0)
            out[oy0:oy0 + hh, ox0:ox0 + ww] = o[cy0:cy0 + hh, cx0:cx0 + ww]
    return _PILImage.fromarray(out)
