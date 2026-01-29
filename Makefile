run:
	python src/main.py
test:
	pytest
setup:
	pip install -r requirements.txt
	python -c "import site, pathlib; p=pathlib.Path(site.getsitepackages()[0])/'wallet_chain_core.pth'; p.write_text(str((pathlib.Path.cwd()/'src').resolve()))"
	pre-commit install
check:
	python -m black .
	python -m isort .
	python -m flake8 .
	python -m pytest
