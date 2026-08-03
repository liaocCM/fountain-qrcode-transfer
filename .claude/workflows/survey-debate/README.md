# survey-debate（dynamic workflow，非 skill）

介於 `deep-research`（10+ agents、全面掃描）和「單人邊查邊想」之間的中量級研究工作流：先分角度平行調查，再逐條驗證來源、對抗式反駁，最後把有憑有據的結論寫成報告。適合工具／模型／方案選型這類「角度能事先列出來、又值得有人挑刺」的調查。

> [!note] 這不是 skill
> `workflow.js` 是一個 [dynamic workflow](https://code.claude.com/docs/zh-TW/workflows)，經 symlink 到 `~/.claude/workflows/survey-debate.js` 成為具名命令 `/survey-debate`。本檔只是說明文件。（曾短暫同時註冊為 skill，因與 workflow 同名造成入口歧義而移除。）

## Pipeline（4 個階段）

| 階段 | 做什麼 | agents |
|---|---|---|
| **Research** | 2–4 個 researcher 平行，各查一個角度，各吐 claim + 來源 | N（預設 2） |
| **Verify** | 逐條 claim 平行驗證：`WebFetch` 打開每個來源，確認來源存在且真的支撐這條 claim；站不住的直接剔除 | 每條 claim 一個（`haiku`） |
| **Debate** | 1 個 skeptic 只攻擊「通過驗證」的 claim 的推理面：矛盾、過時、過度推論 | 1 |
| **Synthesize** | 只用 confirmed 的 claim 寫結論，報告落檔，inline 只回摘要 | 1 |

Verify 排在 Debate 之前是刻意的：先把捏造／誤讀的來源剔掉，讓唯一的 debater 把力氣花在推理，而不是在辯論一條來源根本是假的 claim。驗證是逐條獨立的，天生適合平行；辯論是 barrier（單一 agent 要看到全部 claim 才能抓跨 researcher 的矛盾）。

## 設計原則

- **先驗證來源，再辯論** — Verify 做客觀 grounding（來源在不在、有沒有支撐），Debate 做推理（結論合不合理）。兩種失敗模式分開處理：捏造的來源靠 Verify 擋，站不住的推論靠 Debate 擋。
- **Debate 勝過 consensus** — debater 的 prompt 是「REFUTE, not agree」，預設 `weak`，防止退化成第三個 researcher。這一點有研究支持：debate 型 pattern 一致優於追求共識的 pattern。
- **報告落檔、不 inline** — 完整報告寫到 `/tmp/survey-debate-<slug>.md`，inline 只回路徑 + ≤5 行摘要，避免 output-token 爆掉整個 turn。

## 怎麼跑

主要方式是具名 workflow（跟 `/deep-research` 一樣是斜線命令）：

```
/survey-debate <收斂好的研究問題>
```

或由 agent 呼叫 **Workflow** tool：

```
Workflow({ name: "survey-debate", args: { question: "...", researchers: 2 } })
Workflow({ scriptPath: "/Users/texliao/code/AI/luxray/workflows/survey-debate/workflow.js", args: "..." })
```

- `args` 可以直接是問題字串，或 `{ question, researchers }`（researchers 2–4，預設 2）。
- 問題要先收斂（部署限制、輸入形狀、要回答的清單）— 跟 `deep-research` 一樣，垃圾進垃圾出。
- canonical 檔案在 luxray；`~/.claude/workflows/` 只是 symlink，改動一律改這裡。

## 成本控制

- **Model**：workflow agents 繼承 session model — 先 `/model sonnet` 再跑，跑完切回。Verify 已釘死用 `haiku`，不受影響。
- **規模**：`researchers` 參數（角度依序為：主流方案 → 社群實測 → 替代路線 → 授權／部署現實）。注意 Verify 的 agent 數等於 claim 總數（researcher 數 × 每人 3–8 條），比舊版的固定 4 個多，但都是 `haiku` 且平行。
- **硬上限**：訊息尾端加 `+100k` 之類的 token 預算指令，預算耗盡 agent 就開不出來。

## 什麼時候別用

- 單點事實查證 → 直接查，別開 workflow。
- 探索式、路徑不可預測（下一步取決於上一步）→ 單一 agent 邊查邊調整。
- 要全面、多輪、大量引用的正式報告 → `deep-research`。
