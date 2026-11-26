"""Localized templates for generated course Skills and behavior trials."""

# ruff: noqa: RUF001

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SkillTemplate:
    locale: str
    # SKILL.md section headings
    h_operating: str
    h_empty: str
    h_context: str
    h_interaction: str
    h_evidence: str
    h_prerequisites: str
    h_principles: str
    h_capability: str
    h_paths: str
    h_additional: str
    h_scope: str
    # SKILL.md body strings
    operating_intro: str
    language_line: str
    empty_instruction: str
    empty_guardrail: str
    context_intro: str
    context_skip: str
    learn_rule: str
    practice_rule: str
    apply_rule: str
    reference_rule: str
    progress_rule: str
    evidence_files: str
    evidence_visuals: str
    evidence_outside: str
    evidence_label: str
    cap_learn: str
    cap_practice: str
    cap_apply: str
    cap_reference: str
    cap_rationale: str
    recommended_badge: str
    topics_label: str
    curriculum_rationale: str
    behaviors_label: str
    no_additional: str
    after_attempt_note: str
    no_limitations: str
    unverified_coverage: str
    verified_coverage: str
    scope_derivative: str
    scope_files: str
    # source-map.md
    source_map_title: str
    source_map_intro: str
    meta_speaker: str
    meta_materiality: str
    meta_disposition: str
    meta_confidence: str
    meta_status: str
    status_inferred: str
    status_observed: str
    meta_modalities: str
    meta_disposition_reason: str
    meta_merged_into: str
    relations_heading: str
    # sources.md
    sources_title: str
    sources_intro: str
    coverage_heading: str
    coverage_table_header: str
    unknown_creator: str
    accounting_heading: str
    accounting_unverified: str
    accounting_state: str
    accounting_inputs: str
    accounting_reports: str
    accounting_table_header: str
    claim_evidence_heading: str
    # Behavior trial prompts
    prompt_start: str
    prompt_teach: str
    prompt_exercise: str
    prompt_apply: str
    prompt_reference: str
    prompt_out_of_scope: str
    prompt_opening_thesis: str
    prompt_middle_example: str
    prompt_qualification: str
    prompt_misconception: str
    prompt_prediction: str
    prompt_unresolved: str
    prompt_visual: str


_EN = SkillTemplate(
    locale="en",
    h_operating="How to use this skill",
    h_empty="Empty invocation",
    h_context="Initial context",
    h_interaction="Interaction behavior",
    h_evidence="Evidence and uncertainty",
    h_prerequisites="Prerequisites",
    h_principles="Core principles",
    h_capability="Capability profile",
    h_paths="Learning paths",
    h_additional="Additional material",
    h_scope="Scope and limits",
    operating_intro=(
        "Act as a grounded teacher and practitioner for this course. Match the user's goal "
        "automatically (learning, practice, application, or quick lookup) instead of making "
        "them pick a mode. Load only the smallest relevant file and stay honest about what "
        "the source actually covers."
    ),
    language_line=(
        "Artifacts are written in {language}. Respond in the user's language unless they ask otherwise."
    ),
    empty_instruction=(
        "When invoked with no specific request, load no supporting file, inspect no project, "
        "run no command, and create no file. Reply with this welcome and wait:"
    ),
    empty_guardrail=(
        "Keep the welcome brief and offer `start`. You may mention 2–3 core topics from this "
        "page if helpful, but do not open other files or dump a full syllabus."
    ),
    context_intro=(
        "Once the user starts, ask only the missing questions below (at most three in one turn; "
        "short answers are enough):"
    ),
    context_skip="Skip these questions if the user already gave enough context or asked something specific.",
    learn_rule=(
        "- **Learn:** teach one concept at a time with a concrete example when available, "
        "ask one short check question, and adapt to the answer. Do not dump a whole chapter unless asked."
    ),
    practice_rule=(
        "- **Practice:** give a task and success criteria without loading its solution. "
        "Review the attempt, give the smallest helpful hint, allow a retry, and show the rubric "
        "or solution only after an attempt or explicit request."
    ),
    apply_rule=(
        "- **Apply:** ask only for the missing parts of the user's actual context, map the "
        "course's assumptions to it, mark adaptations as inferred, and verify with observable "
        "checks. Do not present conceptual advice as a verified step-by-step procedure."
    ),
    reference_rule=(
        "- **Reference:** answer first, load the smallest matching file or [source-map.md](source-map.md), "
        "and cite source timestamps for important claims."
    ),
    progress_rule="Keep learner progress in the conversation or host memory, never inside this Skill folder.",
    evidence_files=(
        "Use `provenance.json` for exact claim links, [source-map.md](source-map.md) for full "
        "source details, and [sources.md](sources.md) for coverage accounting. Claims about "
        "what appeared on screen require visual evidence; actions require before-and-after "
        "frames. Never turn low-confidence or inferred notes into firm rules."
    ),
    evidence_visuals=(
        "Open an image under `assets/` only when the active file links it and visual state "
        "matters; do not preload the `assets/` directory."
    ),
    evidence_outside=(
        "Ground answers in the course first and clearly mark inferences. Keep outside or "
        "current knowledge separate from what the source says; ask before taking external "
        "actions or reading private data."
    ),
    evidence_label="Evidence",
    cap_learn="Learn",
    cap_practice="Practice",
    cap_apply="Apply",
    cap_reference="Reference",
    cap_rationale="Rationale",
    recommended_badge=" **Recommended**",
    topics_label="Topics",
    curriculum_rationale="Curriculum rationale",
    behaviors_label="Used for",
    no_additional="- No additional indexed files.",
    after_attempt_note=(
        "- Solutions and answer-bearing rubrics are kept separate; open them only after an "
        "attempt or explicit request."
    ),
    no_limitations="- No additional source-specific limitations recorded.",
    unverified_coverage=(
        "- Full-course coverage was not verified against a workspace; do not claim every "
        "course item is covered."
    ),
    verified_coverage="- Source coverage status: **{state}**.",
    scope_derivative=(
        "- This Skill contains synthesized teaching material, not raw video, full subtitles, "
        "or the local workspace."
    ),
    scope_files=(
        "- See [source-map.md](source-map.md) for full topic coverage, [sources.md](sources.md) "
        "for source status, `provenance.json` for claim links, and `build-manifest.json` for "
        "build metadata."
    ),
    source_map_title="Source map",
    source_map_intro=(
        "Full chronological index of ideas, examples, caveats, predictions, and open questions "
        "from the source material."
    ),
    meta_speaker="Speaker",
    meta_materiality="Importance",
    meta_disposition="Status",
    meta_confidence="Confidence",
    meta_status="Origin",
    status_inferred="inferred",
    status_observed="observed",
    meta_modalities="Modalities",
    meta_disposition_reason="Note",
    meta_merged_into="Merged into",
    relations_heading="Relations",
    sources_title="Sources and coverage",
    sources_intro=(
        "Check course coverage, find original timestamps, or verify a claim. Partial or failed "
        "sources are listed explicitly."
    ),
    coverage_heading="Course sources",
    coverage_table_header="| Source | Creator | Platform | Coverage | Notes |",
    unknown_creator="Unknown",
    accounting_heading="Course accounting",
    accounting_unverified=(
        "**Unverified:** built without a workspace coverage ledger; do not claim complete coverage."
    ),
    accounting_state="- Course coverage: **{state}**",
    accounting_inputs="- Inputs checked: {count}",
    accounting_reports="- Inspection reports: {count}",
    accounting_table_header="| Item | Kind | Status | Coverage | Source ID |",
    claim_evidence_heading="Claim timestamps",
    prompt_start="start",
    prompt_teach="Teach me from the beginning.",
    prompt_exercise="Give me an exercise.",
    prompt_apply="Apply this to my project.",
    prompt_reference="What did the speaker say about the central course idea?",
    prompt_out_of_scope="Using only this course, tell me today's weather at my current location.",
    prompt_opening_thesis="What is the source's opening thesis?",
    prompt_middle_example="What representative example does the source use in the middle of the material?",
    prompt_qualification="What important qualification does the source place on its main advice?",
    prompt_misconception="Correct a likely learner misconception about the course's central guidance.",
    prompt_prediction="What prediction does the source make, and how should I treat it now?",
    prompt_unresolved="Which important question does the source leave unresolved?",
    prompt_visual="Explain one claim that depends on visible state or temporal evidence.",
)

_ZH = SkillTemplate(
    locale="zh",
    h_operating="使用方式",
    h_empty="直接调用时",
    h_context="开始前了解背景",
    h_interaction="互动规则",
    h_evidence="证据与边界",
    h_prerequisites="前置知识",
    h_principles="核心原则",
    h_capability="能力深度",
    h_paths="学习路径",
    h_additional="补充资料",
    h_scope="适用范围与限制",
    operating_intro=(
        "作为本课程的老师与实战搭档，根据用户意图自动切换学习、练习、实战应用或速查，"
        "不要让用户手动选模式。只加载当前需要的最小文件，严格区分课程原意与延伸推断。"
    ),
    language_line="课程资料使用 {language} 编写；默认使用用户的对话语言回复，除非用户另有要求。",
    empty_instruction=(
        "当用户直接调用且没有提出具体需求时，不读取任何附属文件、不检查项目目录、"
        "不执行命令、不创建文件。只回复下面这段欢迎语并等待用户回应："
    ),
    empty_guardrail=(
        "欢迎语保持简短并提示可回复 `start` 开始；可顺带提 2-3 个本页已列出的核心主题，"
        "但不要打开其他文件或倾倒完整大纲。"
    ),
    context_intro="用户开始后，只追问缺失的背景问题（单轮最多三个，简短回答即可）：",
    context_skip="如果用户已经提供了足够背景，或直接问了具体问题，则跳过提问直接进入主题。",
    learn_rule=(
        "- **学习**：一次只讲一个核心点，尽量配一个课程里的具体例子，再抛一个简短检验问题，"
        "根据回答调整节奏。除非用户要求，否则不要一次性甩出整章内容。"
    ),
    practice_rule=(
        "- **练习**：先给出题目和完成标准，不要提前加载答案或评分细则。等用户作答后再自然点评，"
        "优先给最小提示并允许重试；只在作答后或用户明确要求时才展示解析与评分标准。"
    ),
    apply_rule=(
        "- **实战应用**：先向用户了解其实际项目的关键缺失信息，再把课程方法映射到真实场景，"
        "明确标注哪些是推断适配，并给出可观察的验证点。不要把概念性建议包装成已验证的操作步骤。"
    ),
    reference_rule=(
        "- **速查**：先直接给结论，按需打开最小的对应文件或 [source-map.md](source-map.md)，"
        "关键结论附上视频时间戳。"
    ),
    progress_rule="学习进度只保存在当前对话或宿主记忆中，不要写入本 Skill 目录。",
    evidence_files=(
        "精确证据链接见 `provenance.json`，完整主题索引见 [source-map.md](source-map.md)，"
        "来源覆盖记录见 [sources.md](sources.md)。涉及画面状态的结论必须有视觉证据，"
        "操作步骤必须有前后帧支撑；低置信度或推断内容绝不写成铁律。"
    ),
    evidence_visuals=(
        "只有当正在阅读的文档链接了 `assets/` 下的配图且画面细节有帮助时再打开该图，"
        "不要预加载整个 `assets/` 目录。"
    ),
    evidence_outside=(
        "优先基于课程内容作答并自然标注推断；需要补充课外或最新知识时，要与课程原意明确区分。"
        "涉及外部操作或隐私数据时先征得用户同意。"
    ),
    evidence_label="证据",
    cap_learn="学习教学",
    cap_practice="练习反馈",
    cap_apply="实战应用",
    cap_reference="速查参考",
    cap_rationale="说明",
    recommended_badge=" **推荐**",
    topics_label="主题",
    curriculum_rationale="路径说明",
    behaviors_label="适用场景",
    no_additional="- 无其他额外资料。",
    after_attempt_note="- 答案与评分细则单独存放，仅在用户尝试后或明确要求时再打开。",
    no_limitations="- 暂无其他来源限制。",
    unverified_coverage="- 未经工作区校验完整课程覆盖率，请勿宣称已覆盖全部内容。",
    verified_coverage="- 来源采集状态：**{state}**。",
    scope_derivative="- 本 Skill 仅包含提炼后的教学资料，不含原始视频、完整字幕或本地工作区。",
    scope_files=(
        "- 完整主题索引见 [source-map.md](source-map.md)，来源状态见 [sources.md](sources.md)，"
        "证据映射见 `provenance.json`，构建信息见 `build-manifest.json`。"
    ),
    source_map_title="课程内容索引",
    source_map_intro="按时间顺序保留课程中的核心观点、案例、前提条件、预测与悬而未决的问题。",
    meta_speaker="主讲人",
    meta_materiality="重要度",
    meta_disposition="收录状态",
    meta_confidence="置信度",
    meta_status="来源性质",
    status_inferred="推断",
    status_observed="原片直接呈现",
    meta_modalities="证据模态",
    meta_disposition_reason="备注",
    meta_merged_into="合并至",
    relations_heading="逻辑关系",
    sources_title="来源与覆盖记录",
    sources_intro="用于核对课程覆盖范围、定位原视频时间戳或核查结论依据。失败或部分采集的条目会如实列出。",
    coverage_heading="课程来源",
    coverage_table_header="| 来源 | 作者 | 平台 | 覆盖状态 | 备注 |",
    unknown_creator="未知",
    accounting_heading="采集明细",
    accounting_unverified="**未校验**：本包未绑定工作区采集账本，请勿宣称覆盖全部课程。",
    accounting_state="- 采集覆盖状态：**{state}**",
    accounting_inputs="- 输入条目数：{count}",
    accounting_reports="- 检视报告数：{count}",
    accounting_table_header="| 条目 | 类型 | 状态 | 覆盖度 | 来源 ID |",
    claim_evidence_heading="结论时间戳",
    prompt_start="start",
    prompt_teach="从头开始教我这门课。",
    prompt_exercise="给我出一道练习题。",
    prompt_apply="帮我把这门课的方法应用到我的项目里。",
    prompt_reference="主讲人对这门课的核心观点是怎么说的？",
    prompt_out_of_scope="只根据这门课的内容，告诉我我当前位置今天的天气。",
    prompt_opening_thesis="这门课开篇提出的核心论点是什么？",
    prompt_middle_example="课程中段举了什么代表性案例？",
    prompt_qualification="主讲人对核心建议加了哪些重要前提或限制条件？",
    prompt_misconception="请纠正一个学习者在理解这门课核心方法时常见的误区。",
    prompt_prediction="课程里做了什么预测？现在应该如何看待它？",
    prompt_unresolved="课程中留下了哪些重要但未解决的问题？",
    prompt_visual="请结合画面或操作过程证据，解释一个依赖视觉状态的关键结论。",
)

_JA = SkillTemplate(
    locale="ja",
    h_operating="このスキルの使い方",
    h_empty="引数なしの呼び出し",
    h_context="前提の確認",
    h_interaction="対話ルール",
    h_evidence="根拠と境界",
    h_prerequisites="前提知識",
    h_principles="基本原則",
    h_capability="対応レベル",
    h_paths="学習パス",
    h_additional="補足資料",
    h_scope="対象範囲と制限",
    operating_intro=(
        "本コースの講師兼実践パートナーとして、ユーザーの目的に応じて学習・演習・実践・"
        "リファレンスを自動で切り替えます。モード選択を求めず、必要な最小ファイルのみを開いてください。"
    ),
    language_line="資料は {language} で作成されています。特に指定がない限りユーザーの言語で回答してください。",
    empty_instruction=(
        "具体的な依頼なしで呼び出された場合、補足ファイルを読まず、プロジェクトを検査せず、"
        "コマンドを実行せず、ファイルも作成しません。次の案内だけを返して待機してください："
    ),
    empty_guardrail=(
        "案内は簡潔にし、`start` を提示してください。このページ上の主要トピックを 2〜3 件触れても構いませんが、"
        "他のファイルは開かないでください。"
    ),
    context_intro="開始後、不足している背景だけを確認してください（1ターン最大3問、短い回答で十分です）：",
    context_skip="十分な背景や具体的な質問がすでにある場合は、確認を省いて本題に入ってください。",
    learn_rule=(
        "- **学習**：1つずつ要点を説明し、具体例と短い確認質問を1つ添え、回答に合わせて調整します。"
        "求められない限り章全体を一度に出力しないでください。"
    ),
    practice_rule=(
        "- **演習**：解答を読まずに課題と達成基準を示します。回答後に最小限のヒントと再挑戦の機会を与え、"
        "解答や採点基準は試行後または明示的な要求後にのみ提示してください。"
    ),
    apply_rule=(
        "- **実践**：ユーザーの実際の文脈で不足する情報だけを確認し、外部操作の実行やプライベートデータの参照前に必ず同意を得てください。"
        "推測による適用は明示し、検証手順を添えます。"
    ),
    reference_rule=(
        "- **リファレンス**：結論を先に述べ、最小の関連ファイルまたは [source-map.md](source-map.md) を参照し、"
        "重要な主張にはタイムスタンプを添えます。"
    ),
    progress_rule="学習進捗は会話またはホスト記憶に保持し、このスキルフォルダには書き込まないでください。",
    evidence_files=(
        "根拠リンクは `provenance.json`、全体索引は [source-map.md](source-map.md)、"
        "ソース状況は [sources.md](sources.md) を参照します。画面状態の主張には視覚根拠が必要です。"
    ),
    evidence_visuals="`assets/` 内の画像は該当資料からリンクされ必要な場合のみ開き、ディレクトリ全体は先読みしないでください。",
    evidence_outside=(
        "コース根拠を優先し、推測は明示します。外部・最新知識を使う場合はコース内容と区別してください。"
    ),
    evidence_label="根拠",
    cap_learn="学習",
    cap_practice="演習",
    cap_apply="実践",
    cap_reference="リファレンス",
    cap_rationale="理由",
    recommended_badge=" **推奨**",
    topics_label="トピック",
    curriculum_rationale="カリキュラム理由",
    behaviors_label="用途",
    no_additional="- 追加の索引資料はありません。",
    after_attempt_note="- 解答と採点基準は分離されています。試行後または明示的な要求後にのみ開いてください。",
    no_limitations="- 追加のソース制限はありません。",
    unverified_coverage="- ワークスペースでの網羅性検証が行われていません。全網羅とは主張しないでください。",
    verified_coverage="- ソース収集状況：**{state}**。",
    scope_derivative="- 本スキルは要約・再構成された教材であり、元動画・全字幕・ワークスペースを含みません。",
    scope_files=(
        "- 詳細は [source-map.md](source-map.md)、ソース一覧は [sources.md](sources.md)、"
        "根拠リンクは `provenance.json`、ビルド情報は `build-manifest.json` を参照してください。"
    ),
    source_map_title="ソースマップ",
    source_map_intro="元の教材に含まれる主張・具体例・前提条件・予測・未解決の問いを時系列で整理しています。",
    meta_speaker="話者",
    meta_materiality="重要度",
    meta_disposition="収録状態",
    meta_confidence="信頼度",
    meta_status="区分",
    status_inferred="推測",
    status_observed="直接確認",
    meta_modalities="モダリティ",
    meta_disposition_reason="メモ",
    meta_merged_into="統合先",
    relations_heading="関連",
    sources_title="ソースと網羅状況",
    sources_intro="コースの網羅状況の確認やタイムスタンプ参照に使います。欠損・失敗した項目も明記されます。",
    coverage_heading="ソース一覧",
    coverage_table_header="| ソース | 作成者 | プラットフォーム | 状態 | 備考 |",
    unknown_creator="不明",
    accounting_heading="収集サマリー",
    accounting_unverified="**未検証**：ワークスペース台帳なしで生成されました。",
    accounting_state="- 収集状態：**{state}**",
    accounting_inputs="- 入力数：{count}",
    accounting_reports="- 検査レポート数：{count}",
    accounting_table_header="| 項目 | 種別 | 状態 | 網羅度 | ソース ID |",
    claim_evidence_heading="主張タイムスタンプ",
    prompt_start="start",
    prompt_teach="最初から教えてください。",
    prompt_exercise="練習問題を出してください。",
    prompt_apply="この手法を自分のプロジェクトに適用してください。",
    prompt_reference="コースの中心的な主張について話者は何と言っていますか？",
    prompt_out_of_scope="このコースの内容だけで、現在地の今日の天気を教えてください。",
    prompt_opening_thesis="冒頭で示された中心的な主張は何ですか？",
    prompt_middle_example="中盤で使われた代表的な具体例は何ですか？",
    prompt_qualification="主な助言に付けられた重要な前提条件や制限は何ですか？",
    prompt_misconception="中心的な方針について学習者が陥りやすい誤解を訂正してください。",
    prompt_prediction="どのような予測が述べられており、現在どう扱うべきですか？",
    prompt_unresolved="未解決のまま残された重要な問いは何ですか？",
    prompt_visual="画面や操作の根拠に依存する主張を1つ説明してください。",
)


_ZH_LANGUAGE_ALIASES = frozenset(
    {
        "zh",
        "chinese",
        "simplified chinese",
        "traditional chinese",
        "mandarin",
        "中文",
        "简体中文",
        "繁體中文",
        "繁体中文",
        "汉语",
    }
)
_JA_LANGUAGE_ALIASES = frozenset({"ja", "jp", "japanese", "日本語", "にほんご"})


def template_for_language(language: str | None) -> SkillTemplate:
    """Select the closest built-in template for an artifact language tag or name."""
    if not language:
        return _EN
    normalized = " ".join(language.strip().casefold().split())
    tag = normalized.replace("_", "-")
    primary = tag.split("-", 1)[0]
    if primary == "zh" or normalized in _ZH_LANGUAGE_ALIASES:
        return _ZH
    if primary == "ja" or normalized in _JA_LANGUAGE_ALIASES:
        return _JA
    return _EN
