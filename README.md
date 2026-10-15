# personalAIDocker

两个 Runpod Serverless 生图工作流，跑在 ComfyUI 上。两个模型都是**开源权重、无审查**（pipeline 里没有 safety checker，也没有 prompt 过滤）。

| | anime | qwen21 |
|---|---|---|
| 模型 | **Anima** 2B<br>CircleStone Labs + Comfy Org，基于 NVIDIA Cosmos-Predict2 | **Qwen-Image-2.1** 7B<br>阿里，2026-09-20 发布 |
| 定位 | 日系动漫画风 | 通用生图 |
| 文生图 | ✅ | ✅ |
| 图生图 | ✅ 单图 img2img（`denoise` 控重绘强度） | ✅ **1~10 张参考图** + 文本 |
| 显存 | 官方标称 8GB（6GB 可跑） | 24GB（INT8 权重 16.1GB） |
| 镜像大小 | ≈45 GB | ≈56 GB |
| 建议 GPU | `AMPERE_16` $0.58/hr | `AMPERE_24` $0.69/hr |
| 许可 | CircleStone Labs Non-Commercial | Qwen Research License |

> 两者**产出的图片都可以商用**；限制的只是「把模型本身拿去卖服务」。
> 要商用售卖就换：anime → Animagine XL 3.1（openrail++），qwen21 → FLUX.2 [klein] 4B（Apache-2.0）。

---

## 在 Runpod 上部署

两个 endpoint 共用这个仓库，**各自指定不同的 Dockerfile 路径**：
anime 用 `/anime/Dockerfile`，qwen21 用 `/qwen21/Dockerfile`。

1. Runpod 控制台 → **Settings → Connections → GitHub** → Connect，授权本仓库
2. **Serverless → New Endpoint → Import Git Repository** → 选 `personalAIDocker`
3. 第一次建 **anime**：

   | 配置项 | 值 |
   |---|---|
   | Branch | `main` |
   | Dockerfile Path | `/anime/Dockerfile` |
   | GPU | `AMPERE_16`（RTX A4000 / 2000 Ada 16GB）＋兜底 `AMPERE_24` |
   | Container Disk | **60 GB** |
   | Active / Max Workers | 0 / 2 |

4. 再建 **qwen21**，同样流程：

   | 配置项 | 值 |
   |---|---|
   | Dockerfile Path | `/qwen21/Dockerfile` |
   | GPU | `AMPERE_24`（RTX 3090 / L4 / A5000 24GB）＋兜底 `ADA_24` |
   | Container Disk | **75 GB** |
   | Active / Max Workers | 0 / 2 |

> **Container Disk 别开小了。** `runpod/worker-comfyui:5.10.0-base` 解压后是 39.6 GB
> （Docker Hub 上标的 13.7 GB 是压缩传输大小），加上权重后才到 45 / 56 GB。

### 关于控制台的 "Could not find runpod.serverless.start()" 提示

这条黄条可以忽略，但仓库里已经放好对应文件，正常不会再出现：

- 两个镜像都 `FROM runpod/worker-comfyui:5.10.0-base`，Runpod 官方 handler 就在基础镜像里
  （`/handler.py`，由镜像 CMD `/start.sh` 启动），不需要我们自己写 handler。
- 但 Runpod 的 GitHub 集成会**扫仓库里的 Python 代码**判断 endpoint 能不能跑，扫不到就会提示。
  所以仓库根目录放了一份 `handler.py`，是基础镜像内 `/handler.py` 的**字节级副本**
  （sha256 `c43fcc2bfff6cbb60555d7839b902c54de8a9a55ba20caba2bbd3adf9c2886ec`），运行时用的还是镜像里那份。
- 两个 Dockerfile 里各有一行校验，保证镜像里那份和仓库里那份不会走偏；换基础镜像 tag 后
  这里会构建失败，届时把新的 `/handler.py` 重新拷到仓库根目录即可。

### 构建注意

Runpod 的 **`docker build` 步骤上限 30 分钟**（整体 160 分钟）。

- `anime/Dockerfile` 要下 5.4 GB 权重 → 需要约 3 MB/s，基本没问题
- `qwen21/Dockerfile` 要下 16.1 GB 权重 → 需要约 9 MB/s，**有超时风险**

如果 qwen21 构建超时，在仓库里把 `qwen21/Dockerfile` 的 `FROM` 那行改成 `FROM ... AS build` 没用
（不解决时间），正确做法是**打开 W4A8 开关**：把文本编码器从 `qwen3vl_8b_int8_convrot`
（8.71 GB）换成 `qwen3vl_8b_w4a8`（6.41 GB），权重总量从 16.1 GB 降到 13.6 GB。

在 Runpod 的 GitHub 集成里，可以用一个单独的 branch 放改过的 Dockerfile，
然后 endpoint 指向那个 branch（官方支持多环境：production 跟 `main`，staging 跟 `dev`）。

---

## 目录

```
anime/Dockerfile       anime endpoint 镜像
qwen21/Dockerfile      qwen21 endpoint 镜像
handler.py             基础镜像内 /handler.py 的字节级副本（给 Runpod 仓库扫描用，见上）
MODELS.md              模型清单、采样参数、许可说明、下载链接
genpic.py              统一客户端（默认 dry-run，加 --send 才真发）
workflows/
  anime_t2i.json         Anima 文生图        (ComfyUI API 格式)
  anime_img2img.json     Anima 图生图
  qwen21_t2i.json        Qwen-Image-2.1 文生图
  qwen21_multi_edit.json Qwen-Image-2.1 多图编辑 (1~10 张)
```

四个 `workflow.json` 是 ComfyUI 的 **API 格式**，直接当 `input.workflow` 发给 endpoint 的
`/run` 或 `/runsync`。

---

## 调用

```powershell
$env:RUNPOD_API_KEY = "<你的 key>"

# dry-run：先看请求体长什么样
python genpic.py anime --prompt "1girl, solo, white hair"

# 文生图
python genpic.py anime --endpoint-id <ID> --prompt "1girl, solo, white hair" --send

# 图生图
python genpic.py anime --endpoint-id <ID> --prompt "..." --image ref.png --denoise 0.45 --send

# turbo 加速（官方 turbo LoRA，30步 -> 8步）
python genpic.py anime --endpoint-id <ID> --prompt "..." --turbo --send

# Qwen 多图编辑
python genpic.py qwen --endpoint-id <ID> ^
    --prompt "put the logo from <image2> onto the bag in <image1>" ^
    --image bag.png --image logo.png --send
```

| 参数 | anime | qwen21 | 默认 |
|---|---|---|---|
| `--width` / `--height` | ✅ | ✅ | 1024 / 1024 |
| `--steps` | ✅ | ✅ | 30 / 25 |
| `--cfg` | ✅ | ✅（要 >1.0 才生效） | 4.0 / 1.0 |
| `--image` | 单张（给就走 img2img） | 1~10 张 | 无 |
| `--denoise` | ✅ img2img 重绘强度 | — | 0.6 |
| `--turbo` | ✅ 接 turbo LoRA | — | 关 |

---

## 几个已验证的技术细节

这些是实机验证过的，不是推测：

**Anima 的 `CLIPLoader` 用 `type: "stable_diffusion"`** —— 枚举里没有 `anima` 这一项。
`AnimaTEModel` 直接继承 `sd1_clip.SD1ClipModel`，走 SD1 的 CLIP 加载路径，
所以 `stable_diffusion` 是唯一正确的值。

**`comfy update` 不可用** —— 镜像里 ComfyUI 是 `HEAD detached at v0.34.0`，
`git pull` 在游离 HEAD 上必然失败。必须 `git fetch --tags --force` + `git checkout <tag>`。

**Qwen-Image-2.1 的节点在 ComfyUI v0.40.0 才有** —— v0.34.0 只有
`TextEncodeQwenImageEdit` / `...EditPlus`（2025 年的 Qwen-Image-Edit / 2509 用的）。

**两个 VAE 不是一回事** —— Anima 用 `qwen_image_vae.safetensors`（242 MB），
Qwen-Image-2.1 用 `qwen_image_2.1_vae_bf16.safetensors`（644 MB），不能互换。