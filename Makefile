# soliRAG：两个可独立部署的服务
#   center/  联邦中心（控制面）
#   node/    部门节点（Dify 代理）
#
# 常用：
#   make test      全部单测
#   make accept    进程内端到端（零容器）
#   make dev       本地开发栈（1 中心 + 2 节点 + 假 Dify）

PY ?= python

.PHONY: help test test-center test-node accept smoke local stop dev down logs ps

help:
	@echo "make test         跑全部单测（center + node）"
	@echo "make test-center  只跑 center"
	@echo "make test-node    只跑 node"
	@echo "make accept       进程内端到端（零网络零容器）"
	@echo "make local        本机起 4 个真实进程（Ctrl+C 结束）"
	@echo "make smoke        对真实进程跑冒烟（真 uvicorn / 真 TCP）"
	@echo "make dify-setup   在真 Dify 里建知识库 API key 并生成节点配置"
	@echo "make dify-check   真 Dify 三段验证（中心→Dify / Dify→中心 / 应用问答）"
	@echo "make stop         停掉本机进程"
	@echo "make dev          起容器开发栈：1 中心 + 2 节点 + 假 Dify"
	@echo "make down         停容器开发栈"
	@echo "make logs         看容器日志"

test: test-center test-node

test-center:
	cd center && $(PY) -m pytest -q

test-node:
	cd node && $(PY) -m pytest -q

accept:
	$(PY) scripts/accept.py

smoke:
	$(PY) scripts/smoke_real.py

# ── 真 Dify 三段 ──────────────────────────────────────────────
dify-setup:
	$(PY) scripts/dify_setup.py --dataset hr-policy --base-url http://host.docker.internal

dify-check:
	$(PY) scripts/smoke_real_dify.py
	$(PY) scripts/dify_external_kb.py
	$(PY) scripts/dify_app_check.py

local:
	$(PY) scripts/run_local.py

stop:
	$(PY) scripts/run_local.py --stop

dev:
	docker compose -f docker-compose.dev.yml up -d --build

down:
	docker compose -f docker-compose.dev.yml down

logs:
	docker compose -f docker-compose.dev.yml logs -f --tail=100

ps:
	docker compose -f docker-compose.dev.yml ps
