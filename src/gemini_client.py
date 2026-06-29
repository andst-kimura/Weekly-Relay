"""
Gemini API クライアント
議事録要約・転記フォーマット生成・親課題判別に使用する
"""
import logging

logger = logging.getLogger(__name__)

# 親課題判別：キーワード一致ではなく業務文脈で厳密に判断させる
_DETECT_PARENT_PROMPT = """\
以下の業務内容を、最も関連性の高い親課題に紐付けてください。

## 親課題一覧
{parent_issues_text}

## 転記したい業務内容
{content}

## 判定ルール（厳守）
- キーワードの表面的な一致ではなく、業務の目的・文脈・担当領域が一致しているかで判断してください
- 関連性が70%以上確信できる場合のみ親課題キーを返してください
- 複数の親課題に同程度に関連する場合・関連性が不明確な場合・内容が空・不明な場合は「NONE」を返してください
- 最も関連性の高い親課題のISSUEキーを1行だけ返してください（例: SALES_TEAM-27）
- 判定できない場合は「NONE」とだけ返してください
- 説明・前置き・後置きは一切不要です
"""

# Backlogコメントフォーマット生成
_FORMAT_COMMENT_PROMPT = """\
あなたは販売チームの週次進捗報告担当者です。
以下の「データソース」に記載されている情報だけを使って、転記先の親課題の進捗コメントを作成してください。

## 転記先の親課題
{issue_summary}

## データソース（対象週の活動）
{sources_text}

## 出力フォーマット（このフォーマットのみ出力。前置き・後置き不要）

■現在の進捗
・（今週の進捗・合意事項・決定事項・対応状況）
　└（詳細・補足があれば階層表記）
　▼スケジュール（日程が明確な場合のみ）
　　MM/DD〜MM/DD：フェーズ名

■今後のスケジュール
MM/DD：内容
（スケジュール・マイルストーン・締切がある場合のみ記載。なければセクションごと省略）

■リスク共有
・（リスク・懸念事項）
（リスクがない場合は「特になし」）

## 絶対に守るルール
- 【絶対禁止】データソースに記載されていない会議名・議事録・決定事項・スケジュール・合意内容を作り上げること。たとえそれらしい内容でも、データソースに一言も書かれていなければ完全に除外する
- 【絶対禁止】データソースの内容を補完・推測・拡張すること。書かれていないことは存在しないものとして扱う
- データソースが少量（Slack数件のみ等）の場合、それだけを元に書く。情報が不十分なら「■現在の進捗\n・対象期間中の活動なし」とする
- 転記先の親課題のテーマに直接関係する内容のみ抽出する。関係ない内容は除外する
- 日付は締切・マイルストーン・予定スケジュールの場合のみ記載する
- 箇条書きは「・」、サブ項目は「　└」、サブセクションは「　▼」で表現する（「-」「*」は使わない）
- 具体的な課題名・システム名・合意内容・決定事項を含める
"""

# 転記内容の矛盾チェック
_CONSISTENCY_CHECK_PROMPT = """\
以下は複数の Backlog 親課題に転記予定の週次進捗レポートです。
各レポートの内容に事実の矛盾・相互矛盾がないか確認してください。

## 転記予定のレポート一覧
{reports_text}

## 確認ルール
- 同一の事実（日付・決定事項・担当者・数値等）が異なる課題に矛盾する形で記載されていないか
- データソースに存在しない事実（ハルシネーション）が含まれていないか

## 出力フォーマット（このフォーマットのみ出力。前置き・後置き不要）
矛盾なし

または

矛盾あり:
- [課題キー] 問題のある記述: 「...」 → 理由: ...
"""

# 議事録要約
_SUMMARY_PROMPT = """\
以下は Google Meet の議事録です。
次の3セクションを Markdown 形式で出力してください。出力のみ返し、前置き・後置きは不要です。

## 決定事項
- （箇条書き）

## アクションアイテム
| 内容 | 担当者 | 期限 |
|---|---|---|
| ... | ... | ... |

## 議題サマリー
（100文字程度）

---
議事録本文：
{text}
"""

# チケット対応履歴の要約
_TICKET_SUMMARY_PROMPT = """\
以下は Backlog チケットの対応履歴です。
次の形式で簡潔に整理してください。出力のみ返し、前置き・後置きは不要です。

## 対応概要
（このチケットで何をしているかを2〜3文で説明）

## 主な対応内容
- （箇条書きで重要な対応・決定を時系列で列挙。最大8件）

## 現在のステータス
（チケットの現状と残課題を1〜2文で説明）

---
チケット概要: {summary}
ステータス: {status}

対応履歴：
{history}
"""

# Slack スレッド要約
_SLACK_SUMMARY_PROMPT = """\
以下は Slack チャンネルの発言記録です（自分の発言に ★ 印あり）。
次の形式で整理してください。出力のみ返し、前置き・後置きは不要です。

## 主なやりとり
- （箇条書きで重要な話題・決定・依頼を列挙。最大6件）

## 自分のアクション
- （★印の発言から、自分が行った対応・約束・依頼を列挙）

---
チャンネル: #{channel}
期間: {period}

発言記録：
{messages}
"""

# 日次サマリー（Gemini版）
_DAILY_SUMMARY_PROMPT = """\
あなたは社内AIアシスタント Wasabi です。
以下の当日の活動データを元に、夕方の日次サマリーを Slack 向けに自然な文章で作成してください。
出力のみ返し、前置き・後置きは不要です。

## フォーマット
:memo: *Wasabi 日次サマリー — {date}*

*今日の主な活動*
（Backlog・Slack の活動をまとめて3〜5文の自然な文章で。箇条書き不要）

*明日に向けて*
（未完了タスクや次のアクションを1〜2文で）

---
活動データ：
{activities}
"""

# KB 検索結果を文脈に質問回答させるプロンプト（RAG）
_RAG_PROMPT = """\
あなたは社内業務の知識アシスタントです。
以下の「参照ドキュメント」に記載されている情報のみを使って質問に回答してください。

## 参照ドキュメント
{context}

## 質問
{query}

## 回答ルール
- 参照ドキュメントに記載されている情報のみを使って回答する
- 参照ドキュメントに情報がない場合は「KBに該当情報がありません」とのみ答える
- 回答は日本語で簡潔に（300字以内）
- 末尾に情報源（ドキュメントID）を箇条書きで記載する
"""

# Gemini が議事録を生成できなかった場合のフレーズ（フィルタリング用）
_EMPTY_MEETING_PHRASES = [
    "要約は生成されませんでした",
    "会議の要約は生成されません",
    "文字起こしが行われた場合",
    "サポートされている言語での会話量が不足",
    "この会議の詳細は生成されませんでした",
]


def is_empty_meeting_doc(doc: dict) -> bool:
    """議事録の内容が実質的に空（Gemini未生成）かどうか判定"""
    text = doc.get("text") or ""
    if len(text.strip()) < 100:
        return True
    return any(phrase in text for phrase in _EMPTY_MEETING_PHRASES)


class GeminiClient:
    def __init__(self, api_key: str, model: str = "gemini-2.5-flash"):
        self.api_key = api_key
        self.model = model
        self._client = None
        if api_key:
            try:
                from google import genai
                self._client = genai.Client(api_key=api_key)
                logger.info(f"Gemini API 有効: {model}")
            except ImportError:
                logger.warning("google-genai 未インストール。ルールベース要約を使用します")

    @property
    def enabled(self) -> bool:
        return self._client is not None

    def _call(self, prompt: str) -> str:
        """Gemini API を呼び出してテキストを返す共通処理"""
        response = self._client.models.generate_content(
            model=self.model, contents=prompt
        )
        return response.text.strip()

    def detect_parent_issue(self, content: str, parent_issues: list[dict]) -> str | None:
        """
        業務内容テキストから最適な親課題キーを判別して返す。
        判別不能・関連性が低い場合は None を返す。
        parent_issues: [{"issue_key": "SALES_TEAM-27", "summary": "ストアアプリ"}, ...]
        """
        if not self.enabled or not parent_issues:
            return None
        parent_issues_text = "\n".join(
            f"- {p['issue_key']}: {p['summary']}" for p in parent_issues
        )
        prompt = _DETECT_PARENT_PROMPT.format(
            parent_issues_text=parent_issues_text,
            content=content,
        )
        try:
            result = self._call(prompt)
            known_keys = {p["issue_key"] for p in parent_issues}
            if result in known_keys:
                logger.info(f"Gemini親課題判別: {result}")
                return result
            if "NONE" in result:
                return None
            for key in known_keys:
                if key in result:
                    logger.info(f"Gemini親課題判別（抽出）: {key}")
                    return key
            logger.info(f"Gemini親課題判別: 該当なし（応答: {result[:80]}）")
            return None
        except Exception as e:
            logger.warning(f"Gemini 親課題判別失敗: {e}")
            return None

    def format_backlog_comment(self, sources_text: str, issue_summary: str) -> str:
        """
        データソーステキストから =Status= / =NextAction= 形式のコメントを生成する。
        失敗時は空文字を返す（呼び出し元でルールベースにフォールバック）。
        """
        if not self.enabled:
            return ""
        # データが非常に少ない場合（Slack数件のみ）はGeminiへ渡す前に
        # 明示的な警告フレーズを追記し、hallucination を抑止する
        data_lines = [l for l in sources_text.splitlines() if l.strip() and l.startswith("-")]
        if len(data_lines) <= 3:
            sources_text = (
                sources_text
                + "\n\n※ 上記のデータのみが今週の活動記録です。"
                  "これ以外の情報（会議、決定事項、スケジュール等）は存在しません。"
            )
        try:
            prompt = _FORMAT_COMMENT_PROMPT.format(
                issue_summary=issue_summary,
                sources_text=sources_text,
            )
            return self._call(prompt)
        except Exception as e:
            logger.warning(f"Gemini コメント生成失敗（ルールベースにフォールバック）: {e}")
            return ""

    def summarize_meeting(self, text: str) -> str:
        """議事録本文を渡して決定事項・アクションアイテム・サマリーを返す"""
        if not self.enabled:
            return ""
        try:
            prompt = _SUMMARY_PROMPT.format(text=text)
            return self._call(prompt)
        except Exception as e:
            logger.warning(f"Gemini 議事録要約失敗: {e}")
            return ""

    def summarize_ticket(self, summary: str, status: str, history: str) -> str:
        """チケットの対応履歴を要約して対応概要・主な内容・現状を返す"""
        if not self.enabled or not history.strip():
            return ""
        try:
            prompt = _TICKET_SUMMARY_PROMPT.format(
                summary=summary, status=status, history=history
            )
            return self._call(prompt)
        except Exception as e:
            logger.warning(f"Gemini チケット要約失敗: {e}")
            return ""

    def summarize_slack_channel(self, channel: str, period: str, messages: str) -> str:
        """Slack チャンネルの発言記録をやりとり・自分のアクションに整理して返す"""
        if not self.enabled or not messages.strip():
            return ""
        try:
            prompt = _SLACK_SUMMARY_PROMPT.format(
                channel=channel, period=period, messages=messages
            )
            return self._call(prompt)
        except Exception as e:
            logger.warning(f"Gemini Slack要約失敗: {e}")
            return ""

    def check_comment_consistency(self, reports: list[dict]) -> str:
        """
        複数親課題への転記内容に矛盾がないか確認する。
        reports: [{"issue_key": "SALES_TEAM-27", "summary": "...", "comment": "..."}]
        戻り値: "矛盾なし" または 矛盾の説明文
        """
        if not self.enabled or len(reports) < 2:
            return "矛盾なし"
        try:
            lines = []
            for r in reports:
                lines.append(f"### {r['issue_key']} {r['summary']}")
                lines.append(r["comment"])
                lines.append("")
            prompt = _CONSISTENCY_CHECK_PROMPT.format(reports_text="\n".join(lines))
            result = self._call(prompt)
            logger.info(f"矛盾チェック結果: {result[:100]}")
            return result
        except Exception as e:
            logger.warning(f"Gemini 矛盾チェック失敗: {e}")
            return "矛盾なし"

    def answer_with_context(self, query: str, context_docs: list[dict]) -> str:
        """RAG: ChromaDB 検索結果を文脈として質問に回答する。
        context_docs: VectorStore.search() の戻り値リスト
        """
        if not self.enabled or not context_docs:
            return ""
        context_parts = [
            f"[{doc['doc_id']}] (関連度:{doc['score']})\n{doc['text']}"
            for doc in context_docs
        ]
        context = "\n\n---\n\n".join(context_parts)
        try:
            prompt = _RAG_PROMPT.format(context=context, query=query)
            return self._call(prompt)
        except Exception as e:
            logger.warning(f"Gemini RAG回答生成失敗: {e}")
            return ""

    def embed(self, text: str) -> list[float]:
        """テキストを埋め込みベクトルに変換（gemini-embedding-001, 768次元）"""
        if not self.enabled:
            raise RuntimeError("Gemini API が無効です（embed には API キーが必要）")
        result = self._client.models.embed_content(
            model="models/gemini-embedding-001",
            contents=text,
            config={"output_dimensionality": 768},
        )
        return list(result.embeddings[0].values)

    def build_daily_summary(self, date: str, activities: str) -> str:
        """当日の活動データから Slack 向け日次サマリー文を生成する"""
        if not self.enabled or not activities.strip():
            return ""
        try:
            prompt = _DAILY_SUMMARY_PROMPT.format(date=date, activities=activities)
            return self._call(prompt)
        except Exception as e:
            logger.warning(f"Gemini 日次サマリー生成失敗: {e}")
            return ""

