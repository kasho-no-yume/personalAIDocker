# genpic — anime endpoint
#
# 日系动漫画风 · Anima 2B (CircleStone Labs + Comfy Org, 基于 NVIDIA Cosmos-Predict2)
#
# 全套权重 5.4GB，烤进镜像后冷启动不用等下载。
# 用 24GB 以内显卡即可跑（官方标称 8GB，实测 6GB 可跑）。
#
# 基础镜像自带的 ComfyUI 是 v0.34.0 (2026-08-25)，Anima 支持已在内
# （comfy/text_encoders/anima.py、supported_models 里的 Anima 类都已验证存在），
# 所以这个镜像不需要升级 ComfyUI。
#
# RUNPOD BUILD 注意：本镜像约 45 GB，构建需下载 5.4 GB 权重。
# Runpod 的 docker build 步骤上限 30 分钟。

FROM runpod/worker-comfyui:5.10.0-base

ENV HF_HUB_ENABLE_HF_TRANSFER=1

# ---- handler 同步校验 ----
# 基础镜像自带 Runpod 官方 handler：/handler.py，由 /start.sh（镜像的 CMD）启动，
# 本镜像直接继承，运行时用的就是它。
# 仓库根目录另放了一份字节相同的 handler.py，目的是让 Runpod GitHub 集成的仓库扫描
# 能识别到 handler —— 否则控制台会提示
# "Could not find runpod.serverless.start() in your repo"。
# 下面这行保证镜像里那份和仓库里那份不会悄悄走偏；基础镜像换 tag 后这里会失败，
# 届时把新的 /handler.py 重新拷进仓库根目录即可。
RUN test "$(sha256sum /handler.py | cut -d' ' -f1)" = \
      "c43fcc2bfff6cbb60555d7839b902c54de8a9a55ba20caba2bbd3adf9c2886ec" \
      || { echo "CHANGED: /handler.py —— 基础镜像的 handler 变了，请同步仓库根目录的 handler.py"; exit 1; }

# ---- 扩散模型：Anima 2B (Cosmos-Predict2 DiT + LLM adapter) ----
RUN comfy model download \
      --url https://huggingface.co/circlestone-labs/Anima/resolve/main/split_files/diffusion_models/anima-base-v1.0.safetensors \
      --relative-path models/diffusion_models \
      --filename anima-base-v1.0.safetensors

# ---- 文本编码器：Qwen3-0.6B ----
# 注意：这个文件只存在于 circlestone-labs/Anima 仓库，不在 Qwen 官方仓库
RUN comfy model download \
      --url https://huggingface.co/circlestone-labs/Anima/resolve/main/split_files/text_encoders/qwen_3_06b_base.safetensors \
      --relative-path models/text_encoders \
      --filename qwen_3_06b_base.safetensors

# ---- VAE：Qwen-Image VAE（16 通道 Wan2.1 系）----
# 注意：242MB 这个是 Anima 用的，不是 Qwen-Image-2.1 的 644MB 那个，两者不通用
RUN comfy model download \
      --url https://huggingface.co/circlestone-labs/Anima/resolve/main/split_files/vae/qwen_image_vae.safetensors \
      --relative-path models/vae \
      --filename qwen_image_vae.safetensors

# ---- 官方 turbo LoRA：30 步/CFG4 -> 8 步/CFG1 ----
# 只有 142MB，顺手带上，免得回头为了加它重建一次镜像
RUN comfy model download \
      --url https://huggingface.co/circlestone-labs/Anima-Official-LoRAs/resolve/main/anima-turbo-lora-v0.2.safetensors \
      --relative-path models/loras \
      --filename anima-turbo-lora-v0.2.safetensors

# ---- 自检：权重和节点都到位，缺一个就直接构建失败 ----
RUN set -eux; \
    for f in \
      /comfyui/models/diffusion_models/anima-base-v1.0.safetensors \
      /comfyui/models/text_encoders/qwen_3_06b_base.safetensors \
      /comfyui/models/vae/qwen_image_vae.safetensors \
      /comfyui/models/loras/anima-turbo-lora-v0.2.safetensors ; do \
      test -s "$f" || { echo "MISSING: $f"; exit 1; }; \
      ls -lh "$f"; \
    done; \
    echo "--- 节点检查 ---"; \
    test -f /comfyui/comfy/text_encoders/anima.py \
      || { echo "MISSING: anima.py —— 这个 ComicUI 版本不支持 Anima"; exit 1; }; \
    grep -rq "class Anima(" /comfyui/comfy/supported_models.py \
      || { echo "MISSING: Anima 模型类"; exit 1; }; \
    grep -q '"stable_diffusion"' /comfyui/nodes.py \
      || { echo "MISSING: CLIPLoader stable_diffusion 类型"; exit 1; }; \
    echo "自检通过"