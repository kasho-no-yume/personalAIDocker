# 模型清单

两个工作流都跑在 `runpod-workers/worker-comfyui` v5.11.0（Runpod 官方，14,666 次部署）上，
模型放网络卷，serverless 会挂在 `/runpod-volume`，ComfyUI 自动从 `models/` 子目录发现权重。

---

## A. 日系动漫画风 — Anima

`circlestone-labs/Anima`，CircleStone Labs + Comfy Org 联合，基于 NVIDIA Cosmos-Predict2。
**2B 参数，全套 5.4GB，官方标称 8GB 显存（社区实测 6GB 可跑）。**
模型权重里没有任何 safety checker / prompt 过滤。

### 必需（5.38 GB）

| 文件 | 卷内目录 | 大小 | 下载 |
|---|---|---|---|
| `anima-base-v1.0.safetensors` | `models/diffusion_models/` | 3.89 GB | [circlestone-labs/Anima](https://huggingface.co/circlestone-labs/Anima/resolve/main/split_files/diffusion_models/anima-base-v1.0.safetensors) |
| `qwen_3_06b_base.safetensors` | `models/text_encoders/` | 1.11 GB | [circlestone-labs/Anima](https://huggingface.co/circlestone-labs/Anima/resolve/main/split_files/text_encoders/qwen_3_06b_base.safetensors) |
| `qwen_image_vae.safetensors` | `models/vae/` | 242 MB | [circlestone-labs/Anima](https://huggingface.co/circlestone-labs/Anima/resolve/main/split_files/vae/qwen_image_vae.safetensors) |

### 可选加速（142 MB）

| 文件 | 卷内目录 | 说明 |
|---|---|---|
| `anima-turbo-lora-v0.2.safetensors` | `models/loras/` | 官方 turbo LoRA，接上后 30 步/CFG 4 → **8 步/CFG 1**。`genpic.py anime --turbo` |

### 可选换模型（各 3.89 GB，同目录，`--model` 切换）

| 文件 | 说明 |
|---|---|
| `anima-aesthetic-v1.1.safetensors` | 审美向，构图更讲究 |
| `anima-turbo-v1.1.safetensors` | 官方蒸馏加速版（不是 LoRA，是独立 checkpoint） |

### 可选更新知识（5.57 GB）

| 文件 | 说明 |
|---|---|
| `fink94/Anima-2.9B-preview-v1.safetensors` | [fink94/Anima-2.9B](https://huggingface.co/fink94/Anima-2.9B) —— 扩层版，训练数据知识截止 **2026 年 7 月**（base v1.0 是 2025 年 9 月） |

### 采样基线（取自 Comfy 官方模板 `image_anima_base_v1`）

| 参数 | 值 |
|---|---|
| 分辨率 | 1MP~2MP，1024×1024 / 896×1152 / 1152×896 |
| steps | 30（turbo LoRA 时 8） |
| cfg | 4.0（turbo LoRA 时 1.0） |
| sampler / scheduler | `euler` / `simple` |
| negative | `worst quality, low quality, score_1, score_2, score_3, blurry, jpeg artifacts, sepia` |
| 图生图 denoise | 0.45~0.75 |

> 社区也常用 `er_sde` 采样器，说是更稳。先按官方模板的 `euler` 跑，不满意再换。

### 许可（重要）

**CircleStone Labs Non-Commercial License**，并且是 NVIDIA Cosmos-Predict2 的 Derivative Model。
许可明文禁止的包括：
- 把模型架在 API 后面收费
- 托管在付费图像生成平台上
- 作为变现产品的一部分使用

**自己用 / 内部用没问题；产出的图片本身不受限，可以商用。**
如果你打算把这个 endpoint 直接卖给别人或做成付费服务，这个模型不能用。

---

## B. 通用生图 / 多图编辑 — Qwen-Image-2.1

`Qwen/Qwen-Image-2.1`，2026-09-20 发布。**7B Single-Stream DiT（32 层）+ Qwen3-VL-8B 编码器**
（原版 Qwen-Image 是 20B MMDiT，这个瘦身很多）。
一个权重同时做文生图 + 编辑 + **最多 10 张参考图** + 透明通道 PNG + 原生 2K。

在 Artificial Analysis 图生图榜上 Elo **1036**，是目前开源权重里第一（高于 Ideogram 4.0 的 1011、
FLUX.2 [dev] 的 1000）。ComfyUI **首日原生支持**，官方提供 t2i / image-edit / background-removal 三套模板。

### 必需（16.11 GB，取自 Comfy-Org/Qwen-Image-2.1）

| 文件 | 卷内目录 | 大小 |
|---|---|---|
| `qwen_image_2.1_int8_convrot.safetensors` | `models/diffusion_models/` | 6.76 GB |
| `qwen3vl_8b_int8_convrot.safetensors` | `models/text_encoders/` | 8.71 GB |
| `qwen_image_2.1_vae_bf16.safetensors` | `models/vae/` | 644 MB |

选 INT8 ConvRot 而不是 BF16（31.6 GB）就是为了塞进 24GB。精度损失很小，官方模板默认就是这个组合。

### 可选：prompt enhancer（各 8.82 GB，**24GB 卡放不下，默认关**）

| 文件 | 用途 |
|---|---|
| `qwen3.5_9b_qwen_image_2.1_pe_t2i.int8_convrot.safetensors` | 文生图，自动把 prompt 扩写成完整英文描述 |
| `qwen3.5_9b_qwen_image_2.1_pe_i2i.int8_convrot.safetensors` | 编辑指令改写 |

想开的话换 `w4a8` 版（6.41 GB），或者上 48GB 卡。

### 采样基线（取自 Comfy 官方模板）

| 参数 | 值 |
|---|---|
| 分辨率 | 默认 1MP（1024×1024）；原生 2K（2048×2048）也支持，但显存和时间都翻几倍 |
| steps | 模板默认 25，**官方完整管线是 40~50**（euler）。质量敏感就调到 40 |
| cfg | **保持 1.0**。官方路径就是 cfg=1；要上 negative prompt 才提高 |
| sampler / scheduler | `euler` / `simple` |
| 参考图 | 最多 10 张，在 prompt 里用 `<image1>` ... `<image10>` 指代。`image_1` 是编辑目标 |

### 许可（重要）

**Qwen Research License**，限制商用。自用/内部用没问题，**产出的图片不受限**。
同样是：要卖服务的话不能用。

> 顺带说明：现在（2026-10）市面上**新的**无审查生图模型基本都转非商用许可了 ——
> FLUX.2 [dev] 是 FLUX Non-Commercial，FLUX 3 的图像层还没出货（只有视频层），
> Qwen-Image-3.0 的权重仓库返回 401（未开放）。**Apache-2.0 的新选项只剩 FLUX.2 [klein] 4B**
> （2025-11，2 秒/张，12.8GB 显存），就是上一轮我推荐的那个 —— 画质不如 Qwen-Image-2.1，
> 但可商用且快 10 倍。如果你有商用需求，我再补一个 klein 的 endpoint。

---

## 权重放哪

ComfyUI 只认 `models/` 目录下的子目录，权重必须落到对应的子目录里。两条路：

**A. 烤进镜像**（`FROM runpod/worker-comfyui:5.10.0-base` + `comfy model download`）
```
/comfyui/models/
├── diffusion_models/anima-base-v1.0.safetensors
├── text_encoders/qwen_3_06b_base.safetensors
├── vae/qwen_image_vae.safetensors
└── loras/anima-turbo-lora-v0.2.safetensors
```

**B. 网络卷**（serverless 挂载点固定 `/runpod-volume`，ComfyUI 自动发现）
```
/runpod-volume/models/
├── diffusion_models/...
├── text_encoders/...
├── vae/...
└── loras/...
```

必需权重合计 **21.5 GB**。卷建议开 50 GB（留 2 倍余量给可选模型和 LoRA）。

灌权重的三种方式：`wget` 直连 HF / `runpodctl send` 上传 / Pod 里装 `huggingface_hub` 走 API。

> 网络卷的模型发现不是即时的。如果权重放了却找不到，开 `NETWORK_VOLUME_DEBUG=true` 看日志。