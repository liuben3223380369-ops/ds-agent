#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
架构卡片 —— 按需注入最新神经网络架构知识。

【为什么做成"按需注入"而不是常驻提示词】
大多数任务是写业务逻辑、数据处理、工具脚本，此时架构知识是纯噪声，
还会诱发过度设计（明明写个函数，却搭起 MoE + 稀疏注意力）。
所以只在任务确实涉及模型/训练时才注入，且注入的是**工程坑位**而非架构科普。

【内容取向】
不讲"这个架构多先进"，只讲三件事：
  1. 什么条件下值得用（多数情况答案是不值得）
  2. 用了之后哪里最容易炸
  3. 最小可用的骨架长什么样

2026 年前沿要点已核对：混合架构比例、mHC、Muon、MoE 路由、长上下文取舍。
"""

import re

# 命中任一关键词才注入
ML_KEYWORDS = [
    "torch", "pytorch", "tensorflow", "jax", "keras", "transformer",
    "attention", "神经网络", "模型", "训练", "train", "model", "nn.",
    "embedding", "llm", "mamba", "moe", "expert", "微调", "fine-tune",
    "finetune", "推理", "inference", "梯度", "gradient", "loss",
    "cuda", "gpu", "bert", "gpt", "diffusion", "扩散",
]

# 明确在做架构/训练类任务（比 ML_KEYWORDS 更强，用于给更重的卡片）
ARCH_KEYWORDS = [
    "transformer", "attention", "mamba", "ssm", "moe", "mixture of experts",
    "混合架构", "hybrid", "稀疏注意力", "sparse attention", "mhc",
    "muon", "残差", "residual", "长上下文", "long context", "kv cache",
    "状态空间", "state space", "位置编码", "rope", "mla",
]

# 明确不相关的场景，即使出现 model 之类的词也不注入
ANTI_KEYWORDS = ["sort", "快排", "爬虫", "正则", "excel", "csv解析", "重命名文件"]


def is_ml_task(task):
    t = (task or "").lower()
    if not t:
        return False
    if any(a in t for a in ANTI_KEYWORDS) and not any(k in t for k in ARCH_KEYWORDS):
        return False
    return any(k in t for k in ML_KEYWORDS)


def is_arch_task(task):
    t = (task or "").lower()
    return any(k in t for k in ARCH_KEYWORDS)


# ---------------- 卡片内容 ----------------
BASICS = """
【神经网络工程常识 · 按需加载】

先判断要不要上复杂结构：
· 序列短（<32K）且需要精确召回 → 普通 dense attention 就够，且更便宜
· 序列极长（>100K）或流式推理 → 才值得考虑 SSM / 线性注意力 / 混合
· 代码生成、少样本学习、长尾事实召回 → Transformer 仍是当前最优，别换
· 绝大多数"我想用最新架构"的需求 → 实际瓶颈在数据、学习率和输入处理上

训练前必做的稳定性检查（比换架构重要得多）：
1. 先用 1~2 条样本过一遍 forward+backward，确认能跑通再上全量
2. 每 N 步打印 loss，出现 NaN/inf 立刻停，别等跑完
3. 梯度裁剪（clip_grad_norm_）默认开上
4. 学习率做 warmup；换优化器时学习率通常要重调
5. 固定随机种子，保证可复现，否则无法判断改动是否真的有效
6. 用小模型/小数据跑通全流程，再放大规模

调试优先级（按性价比排序）：
  数据/标签 → 输入归一化 → 学习率 → 损失函数 → 模型容量 → 架构创新
架构创新排最后，且只有在前面都确认无问题时才动。
"""

ARCH_2026 = """
【2026 架构前沿 · 工程要点】（仅在确实涉及架构设计时参考）

一、混合架构是当前主流
· 做法：少量 attention 层 穿插 大量 SSM(Mamba-2/3) 层
· 经验比例：约 1 个 attention 层 : 5~8 个 SSM 层
  （Jamba 约 1:8；Nemotron-H 替换掉约 92% 的 attention 层）
· 分工：SSM 负责长序列的廉价处理（O(n)），attention 负责精确召回
· KV cache：只有 attention 层计入，约为全 Transformer 的 1/8 ~ 1/10
· 坑：纯 SSM 在 5-shot、复制/召回类任务上明显弱于 Transformer，
  这是压缩状态的**结构性缺陷**，不是调参能解决的；混合能大幅缩小但不完全消除

二、长上下文压缩（以 DeepSeek V4 的 CSA/HCA 为例）
· CSA：约 4x 序列压缩 + top-k 稀疏选择，保留"清晰的局部视图"
· HCA：约 128x 更激进压缩 + 对压缩块做 dense attention，提供"廉价全局视图"
· 两者在网络中交替出现；另保留一个未压缩的近期 token 滑动窗口（约 128）
· 注意力Sink：让 softmax 之和小于 1，稳定超长序列
· 效果：1M 上下文下 KV cache 降到上一代的约 10%（Pro）/ 7%（Flash）
· 坑：压缩率与召回精度直接冲突，压缩越狠，精确查找越弱

三、mHC（Manifold-Constrained Hyper-Connections）
· 动机：把残差流加宽（如 n_hc=4）能提升容量，但直接堆会数值不稳定
· 做法：用 Sinkhorn-Knopp 把残差混合矩阵投影到双随机矩阵（Birkhoff 多面体），
  训练时约 20 次迭代；输入/输出映射用 sigmoid 限幅
· 为什么有效：双随机矩阵的谱范数 ≤ 1，正反向都非扩张，深层堆叠不会放大信号
· 坑：不约束就等着看 loss 起飞，这不是玄学而是明确的数值问题

四、Muon 优化器（2026 已在万亿参数规模验证）
· 核心：用 Newton-Schulz 迭代对梯度更新做正交化
· 典型配置：8 次收敛迭代 + 2 次稳定迭代
· 分工：大部分权重矩阵用 Muon；embedding、预测头、bias、RMSNorm 用 AdamW
· 坑：从 AdamW 换到 Muon 时学习率不能直接沿用，需重新调

五、MoE 路由
· 已成前沿标配（如 256 experts，每 token 激活 8 routed + 1 shared）
· shared expert 处理通用模式，routed expert 做特化，有助于稳定
· 坑一：expert collapse —— 少数专家垄断，其余饿死；需负载均衡损失
· 坑二：显存按总参数算，不是按激活参数算，别只看激活量就以为能塞进卡

六、MLA（Multi-head Latent Attention）
· 把 KV 投影到低秩隐空间再缓存，推理时再升回来
· KV cache 显著缩小，质量损失小
· 坑：与某些推理加速库的自定义 kernel 兼容性要单独验证

【一句话结论】
如果你的目标是"让代码跑对"，以上都不重要。
如果目标是"训练一个模型"，优先搞定数据与稳定性，架构改动放最后，
且每次只改一处、固定其他变量，否则无法归因。
"""


def build_arch_prompt(task):
    """根据任务生成要注入的架构知识；不涉及则返回空串。"""
    if not is_ml_task(task):
        return ""
    parts = [BASICS]
    if is_arch_task(task):
        parts.append(ARCH_2026)
    return "\n".join(parts)


# ---------------- 最小骨架（可复制修改） ----------------
SKEL_TRAIN = '''
# 最小训练骨架：先把流程跑通，再谈优化
import torch, random, numpy as np

def set_seed(s=42):
    random.seed(s); np.random.seed(s); torch.manual_seed(s)
    torch.cuda.manual_seed_all(s)

set_seed()
model = ...                       # 先用最小规模（如 d_model=64, 2 层）
opt = torch.optim.AdamW(model.parameters(), lr=1e-3)

for step, (x, y) in enumerate(loader):
    out = model(x)
    loss = loss_fn(out, y)

    if not torch.isfinite(loss):          # NaN 早停，别等跑完
        print(f"[FATAL] step {step} loss={loss}"); break

    opt.zero_grad()
    loss.backward()
    torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)   # 默认开
    opt.step()

    if step % 50 == 0:
        print(f"step {step} loss {loss.item():.4f}")   # 必须能看见曲线

    if step >= 20:      # 冒烟测试：20 步能跑通且 loss 有下降趋势再放开
        print("[smoke ok]"); break
'''

SKEL_HYBRID = '''
# 混合架构骨架示意：N 个 SSM 层配 1 个 attention 层
# 注意：真实实现依赖具体库（mamba_ssm 等），此处仅表达结构比例
class HybridBlock(nn.Module):
    def __init__(self, d, n_ssm=7):
        super().__init__()
        self.ssms = nn.ModuleList([SSMLayer(d) for _ in range(n_ssm)])
        self.attn = AttentionLayer(d)          # 每 n_ssm 层后接 1 层
        self.norm = nn.RMSNorm(d)

    def forward(self, x):
        for s in self.ssms:
            x = x + s(self.norm(x))            # 残差必须有
        x = x + self.attn(self.norm(x))
        return x

# 比例是经验值不是定理：先 1:7 起，按召回类评测结果再调
# 判断方法：做一次"长距离精确召回"测试，掉点了就把 attention 层加密
'''


def skeleton_for(task):
    t = (task or "").lower()
    if not is_ml_task(t):
        return ""
    if any(k in t for k in ["mamba", "ssm", "混合", "hybrid", "状态空间"]):
        return SKEL_HYBRID
    return SKEL_TRAIN
