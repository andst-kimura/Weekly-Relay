"""
RAG 検索のゴールデンクエリ回帰テスト

過去に検索品質の問題を起こしたクエリを、検索結果レベル（どのドキュメントが
ヒットするか）で検証する。回答文自体は生成のたびに揺れるため検証しない。

実 API（Gemini・Firestore）を使用するため、通常の pytest 実行では skip される。
実行方法:
    RUN_GOLDEN=1 python -m pytest tests/test_rag_golden.py -v
"""
import os
import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_GOLDEN") != "1",
    reason="実 API を使うため RUN_GOLDEN=1 指定時のみ実行",
)


@pytest.fixture(scope="module")
def bot():
    import sys, io
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from dotenv import load_dotenv
    load_dotenv()
    import urllib3
    urllib3.disable_warnings()
    import truststore
    truststore.inject_into_ssl()
    from main import load_config
    from src.gemini_client import GeminiClient
    from src.vector_store import VectorStore
    from src.slack_bot import SlackBot
    config = load_config()
    g = config["gemini"]
    gemini = GeminiClient(api_key=g["api_key"], model=g["model"])
    vs = VectorStore(embed_fn=gemini.embed, term_expander=gemini.expand_search_terms)
    return SlackBot(bot_token="x", app_token="x", vector_store=vs,
                    gemini_client=gemini, config=config)


class TestGoldenQueries:
    def test_issue_key_direct_hit(self, bot):
        """課題キーを含む質問は該当ドキュメントが先頭（score=1.0）に来る"""
        results = bot.vs.search("CORE_DB-3784 の対応状況を教えて")
        assert results, "検索結果が空"
        assert results[0]["doc_id"].endswith("CORE_DB-3784")
        assert results[0]["score"] == 1.0

    def test_abbreviation_expansion(self, bot):
        """略語（マケプレ）が正式名称に展開されて言及ドキュメントがヒットする"""
        results = bot.vs.search("マケプレの概要と進捗を教えて。")
        joined = " ".join(r["text"] for r in results[:5])
        assert ("マケプレ" in joined or "マーケットプレイス" in joined), \
            f"マケプレ言及ドキュメントが上位にない: {[r['doc_id'] for r in results[:5]]}"

    def test_independent_question_not_polluted_by_history(self, bot):
        """独立した質問は無関係な履歴に汚染されない"""
        dirty_history = (
            "ユーザー: やり取りの中で機密情報はありますか？\n"
            "Bot: Netskope導入プロジェクトにおいて機密情報アップロードの防止が挙げられています。"
        )
        answer, info = bot._answer_query("ストアアプリの進捗を教えて。", history=dirty_history)
        assert info["used_history"] is False, "独立質問なのに履歴が使われた"
        assert info["answered"] is True, f"回答できなかった: {answer[:100]}"
        joined = " ".join(info["doc_ids"])
        assert "STORE_APP" in joined or "ストアアプリ" in " ".join(
            r["meta"].get("source_name", "") for r in info["results"]), \
            f"ストアアプリ文書がヒットしない: {info['doc_ids']}"

    def test_anaphoric_question_uses_history(self, bot):
        """指示語を含む追い質問は履歴で解釈される"""
        history = (
            "ユーザー: 先週のストアアプリ案件で発生した課題を教えて\n"
            "Bot: 一部のストアアプリから呼び出せないAPI（売価情報取得APIやEJ検索用API）があり、"
            "売価情報取得APIは一時的にBizのシステム区分で運用する方針が決定。"
        )
        answer, info = bot._answer_query("そのAPIの課題は解決しましたか？", history=history)
        assert info["used_history"] is True, "指示語があるのに履歴が使われていない"
        assert info["answered"] is True, f"回答できなかった: {answer[:100]}"
