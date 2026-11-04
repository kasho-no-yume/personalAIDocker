# genpic - pony endpoint
# 日系/插画混合风格 · Pony Diffusion v6 XL
# 模型权重烤进镜像，冷启动无需下载
FROM runpod/worker-comfyui:5.10.0-base

ENV HF_HUB_ENABLE_HF_TRANSFER=1

# handler 校验（和仓库保持一致）
RUN test "$(sha256sum /handler.py | cut -d ' ' -f1)" = \
      "c43fcc2bfff6cbb60555d7839b902c54de8a9a55ba20caba2bbd3adf9c2886ec" \
      || { echo "CHANGED: /handler.py"; exit 1; }

# Pony Diffusion v6 XL
RUN comfy model download \
      --url https://huggingface.co/LyliaEngine/Pony_Diffusion_V6_XL/resolve/main/ponyDiffusionV6XL_v6StartWithThisOne.safetensors \
      --relative-path models/checkpoints \
      --filename ponyDiffusionV6XL_v6StartWithThisOne.safetensors

RUN test -s /comfyui/models/checkpoints/ponyDiffusionV6XL_v6StartWithThisOne.safetensors && echo "OK: Pony loaded"
