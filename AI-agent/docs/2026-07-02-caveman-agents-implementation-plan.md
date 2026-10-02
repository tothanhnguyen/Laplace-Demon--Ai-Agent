# Caveman Multi-Agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bật Caveman cho Hermes (`lite`), OpenClaw (`full`) và Codex (`full`) bằng cấu hình có thể kiểm chứng và rollback.

**Architecture:** OpenClaw và Codex dùng installer Caveman chính chủ, ghim release `v1.9.0` bên trong installer. Hermes dùng adapter tối thiểu: reuse skill đã cài cho OpenClaw và thêm bootstrap marker vào `~/.hermes/SOUL.md`.

**Tech Stack:** Bash, Node.js 24, Caveman v1.9.0, Markdown skills, Hermes/OpenClaw SOUL files.

## Global Constraints

- Không cài Caveman cho agent ngoài Hermes, OpenClaw và Codex.
- Không bật hooks phụ, `--with-init` hoặc MCP shrink.
- Giữ nguyên code, command, path, API name và error string.
- Auto-Clarity áp dụng cho security, irreversible actions và chuỗi bước dễ hiểu sai.
- Không tuyên bố giảm input/reasoning token.

---

### Task 1: Cài Caveman chính chủ cho OpenClaw và Codex

**Files:**
- Create: `~/.openclaw/workspace/skills/caveman/SKILL.md`
- Modify: `~/.openclaw/workspace/SOUL.md`
- Create: skill files trong Codex profile do `skills` CLI quản lý

**Interfaces:**
- Consumes: Node.js >=18, OpenClaw workspace, Codex profile
- Produces: Caveman skill discoverable trong OpenClaw và Codex

- [x] **Step 1: Chạy installer riêng cho OpenClaw**

```bash
curl -fsSL https://raw.githubusercontent.com/JuliusBrussee/caveman/main/install.sh |
  bash -s -- --only openclaw --non-interactive
```

Expected: installer ghi skill và bootstrap block vào OpenClaw workspace.

- [x] **Step 2: Chạy installer riêng cho Codex**

```bash
curl -fsSL https://raw.githubusercontent.com/JuliusBrussee/caveman/main/install.sh |
  bash -s -- --only codex --non-interactive
```

Expected: installer báo `codex` trong danh sách installed hoặc already installed.

- [x] **Step 3: Kiểm tra artifact**

```bash
test -f ~/.openclaw/workspace/skills/caveman/SKILL.md
rg -n "caveman-begin|Default intensity" ~/.openclaw/workspace/SOUL.md
find ~/.codex ~/.agents -path '*/caveman*/SKILL.md' -print
```

Expected: ba lệnh tìm thấy OpenClaw skill, một marker block và ít nhất một Codex skill path.

### Task 2: Tạo Hermes adapter mức lite

**Files:**
- Create: `~/.hermes/skills/productivity/caveman/SKILL.md`
- Modify: `~/.hermes/SOUL.md`
- Create: `~/.hermes/SOUL.md.bak.caveman`

**Interfaces:**
- Consumes: OpenClaw Caveman skill từ Task 1
- Produces: Hermes skill discovery và always-on lite instruction

- [x] **Step 1: Backup và copy skill**

```bash
cp ~/.hermes/SOUL.md ~/.hermes/SOUL.md.bak.caveman
mkdir -p ~/.hermes/skills/productivity/caveman
cp ~/.openclaw/workspace/skills/caveman/SKILL.md \
  ~/.hermes/skills/productivity/caveman/SKILL.md
```

Expected: backup và Hermes skill tồn tại.

- [x] **Step 2: Thêm marker block vào Hermes SOUL**

Thêm đúng một block:

```markdown
<!-- caveman-hermes-begin -->
## Caveman lite (always on)

Respond concise in user's language. Remove filler and hedging; keep complete sentences because Hermes produces plans and architecture. Preserve all technical substance, code, commands, paths, API names, and exact errors. Use full clarity for security warnings, irreversible actions, and ordered procedures where brevity risks ambiguity.
<!-- caveman-hermes-end -->
```

- [x] **Step 3: Kiểm tra idempotency và discovery**

```bash
test -f ~/.hermes/skills/productivity/caveman/SKILL.md
test "$(rg -c 'caveman-hermes-begin' ~/.hermes/SOUL.md)" -eq 1
hermes skills list 2>/dev/null | rg -i caveman || true
```

Expected: skill tồn tại, marker count bằng 1; nếu Hermes CLI hỗ trợ listing thì có `caveman`.

### Task 3: Restart và kiểm chứng cấu hình

**Files:**
- Verify: `~/.openclaw/workspace/SOUL.md`
- Verify: `~/.hermes/SOUL.md`

**Interfaces:**
- Consumes: artifacts từ Task 1–2
- Produces: cấu hình active cho session mới

- [x] **Step 1: Restart gateway**

```bash
hermes gateway restart
openclaw gateway restart
```

Expected: cả hai command exit 0.

- [x] **Step 2: Kiểm tra marker và skill lần cuối**

```bash
rg -n "caveman-begin|Default intensity" ~/.openclaw/workspace/SOUL.md
rg -n "caveman-hermes-begin|Caveman lite" ~/.hermes/SOUL.md
test -s ~/.openclaw/workspace/skills/caveman/SKILL.md
test -s ~/.hermes/skills/productivity/caveman/SKILL.md
```

Expected: đủ hai marker, hai skill không rỗng.
