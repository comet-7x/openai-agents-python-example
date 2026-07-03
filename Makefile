.PHONY: install lint format type test check pre-commit clean

install:        ## 安装依赖 + 注册 pre-commit
	uv sync
	uv run pre-commit install

lint:           ## Ruff 检查
	uv run ruff check .

format:         ## Ruff 自动修复 + 格式化
	uv run ruff check --fix .
	uv run ruff format .

type:           ## Pyright 类型检查
	uv run pyright

test:           ## 跑测试 + 覆盖率
	uv run pytest

check: lint type test   ## 本地一键全量检查(等价 CI)

pre-commit:     ## 对所有文件手动跑 pre-commit
	uv run pre-commit run --all-files

clean:          ## 清理缓存
	rm -rf .pytest_cache .ruff_cache .coverage coverage.xml htmlcov
	find . -type d -name __pycache__ -exec rm -rf {} +
