# 検証・運用の環境をそのまま再現するためのイメージ。
# 秘密情報（.env）と相場データ・台帳はイメージに入れない（.dockerignore）。
# データは実行時にフォルダごとつなぐ（Makefile の docker-* を参照）。
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# 依存を先に入れる。コードだけ変えたときに、ここからやり直さずに済む
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install -e ".[dev,gui,gcp]"

COPY scripts ./scripts
COPY tests ./tests
COPY .streamlit ./.streamlit
COPY Makefile .env.example ./

# root では動かさない
RUN useradd --create-home --uid 1000 app && mkdir -p data state && chown -R app:app /app
USER app

# GUI: コンテナの中では全インターフェースで待ち受ける。
# 外への公開は、ホスト側で 127.0.0.1 に絞る（docker run -p 127.0.0.1:8501:8501）
ENV STREAMLIT_SERVER_ADDRESS=0.0.0.0
EXPOSE 8501

CMD ["pytest", "-q"]
