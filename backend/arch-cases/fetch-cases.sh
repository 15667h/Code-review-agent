#!/usr/bin/env bash
# 拉取架构测试场（arch-cases）的上游仓库。
#
# 为什么这些代码不在本仓库里：
#   pluggy / requests / celery 是别人的项目，各有自己的许可证。
#   为了一个评估案例把三个上游仓库整体搬进来，既让仓库变得臃肿，
#   也让许可证边界变模糊。所以这里只记录「用哪个提交」，代码按需克隆。
#
# 用法：
#   bash backend/arch-cases/fetch-cases.sh          # 拉取全部
#   bash backend/arch-cases/fetch-cases.sh e1-pluggy  # 只拉一个
#
# 拉取后即可用：
#   cd backend && python -m harness --agent architect -r arch-cases/e1-pluggy/repo
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
target="${1:-}"

# 案例名 | 上游地址 | 建议分支
cases=(
  "e1-pluggy|https://github.com/pytest-dev/pluggy|main"
  "e2-requests|https://github.com/psf/requests|main"
  "e3-celery|https://github.com/celery/celery|main"
)

for entry in "${cases[@]}"; do
  IFS='|' read -r name url branch <<< "$entry"
  if [[ -n "$target" && "$target" != "$name" ]]; then
    continue
  fi

  dest="$here/$name/repo"
  if [[ -d "$dest" ]]; then
    echo "已存在，跳过：$name"
    continue
  fi

  echo "拉取 $name ← $url"
  # --depth 1：评估只读当前结构，不需要历史
  git clone --depth 1 --branch "$branch" "$url" "$dest" >/dev/null 2>&1 \
    || git clone --depth 1 "$url" "$dest" >/dev/null 2>&1

  # 上游仓库自带的 .git 留着会被 Workspace 当成审计内容的一部分扫进去
  rm -rf "$dest/.git"
  echo "  → $dest（$(find "$dest" -name '*.py' | wc -l) 个 Python 文件）"
done

echo
echo "完成。注意：评估分数与具体提交强相关 ——"
echo "上游结构变了，arch-cases/*/reference.json 里的参考蓝图可能对不上，需要重写。"
