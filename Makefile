run:
	python src/main.py
test:
	pytest
setup:
	pip install -r requirements.txt
	pre-commit install
check:
	python -m black .
	python -m isort .
	python -m flake8 .
	python -m pytest
