# Shared Tooling & Prior-Art Sources — v001
Referenced by committee + researcher prompts. The anti-hallucination defense: novelty/baseline claims
MUST cite sources actually searched here. A "kill" citation counts only if verified in ≥2 independent sources.

## How to actually search (commands available on nodes)
- **Web**: `web_search` tool (titles/URLs/excerpts) — fast first pass for papers, repos, docs, blogs.
- **Meta CLI** (`meta`, on a devvm/control node, after `source /tmp/agentenv.sh`): resolve fburl/docs,
  fetch Google Docs/Sheets, look up internal tasks/diffs. Use for internal context, NOT public novelty.
- **GitHub**: `gh search repos/code/prs/issues "<query>"`, `gh repo view`, clone + grep for mechanism checks.
- **arXiv / Semantic Scholar / OpenAlex / DBLP**: via `web_search` or direct HTTP (curl) to their public APIs.
- **Papers with Code**: leaderboards + linked repos for baseline-state.
- Record EVERY search (query, source, found, ruled-out) in the prior-art search_log (see template).

## Required academic sources (Candidate / Paper-track)
arXiv · Semantic Scholar · Google Scholar (if avail) · DBLP · ACM DL · USENIX (ATC/OSDI/NSDI/FAST) ·
IEEE Xplore (if avail) · MLSys · ASPLOS · SOSP · EuroSys · ISCA/MICRO/HPCA (if HW-relevant) ·
NeurIPS/ICML/ICLR (if ML-method-relevant).

## Required OSS / implementation prior art
GitHub · vLLM · SGLang · LMCache · FlashInfer · TensorRT-LLM · DeepSpeed · Megatron-LM · Ray ·
KServe · Triton Inference Server · HF TGI · llama.cpp · KV-cache repos · CUDA/ROCm examples · NCCL/UCX/RDMA.

## Required vendor / systems docs (when behavior depends on them)
NVIDIA CUDA / NCCL / GPUDirect-RDMA / TensorRT-LLM · AMD ROCm / RCCL · Google TPU · PyTorch · XLA ·
Linux kernel (HugeTLB/mmap/NUMA) where memory-relevant.

## Emerging / unpublished prior art
GitHub issues + PRs · project discussions · workshop papers · credible-lab blogs · benchmark leaderboards ·
artifact-evaluation repos.

## Prior-art freshness (re-search REQUIRED when)
- promoting exploration→candidate or candidate→paper-track; before any full committee review;
- a major new baseline appears; >14 days passed (fast-moving: LLM inference, KV-cache, vLLM/SGLang/
  LMCache/FlashInfer/TensorRT-LLM, GPU memory, agent runtime); >30 days (slower systems topics).

## Anti-hallucination rule (enforced)
- Every "this is prior work" / "baseline X dominates" claim cites a real, retrievable source (URL/cite).
- A novelty KILL requires the collision verified in ≥2 independent sources.
- Separate NAMING novelty from MECHANISM novelty. Unverifiable citation = not admissible.
