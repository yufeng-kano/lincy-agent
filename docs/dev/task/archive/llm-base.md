# LLM 對話基礎架構

建立可切換 LLM provider 的對話系統，支援靈活的上下文工程。

## 背景

專案需要一個穩定的對話基礎，支援多 provider（Ollama、Anthropic、OpenAI、Gemini），
並為之後的上下文工程預留擴充點。使用 httpx 直接呼叫 API，減少依賴。

## 設計決策

### 依賴選擇

- **httpx**：HTTP client，支援 async 和 streaming，不用官方 SDK
- **pyyaml**：讀取配置檔
- **python-dotenv**：自動載入 `.env` 環境變數

### 為何不用官方 SDK（anthropic、openai）

- 減少依賴樹
- 完全掌控請求格式
- 兩家 API 格式穩定，自己呼叫不難
- 統一用 httpx，不用適配兩套 SDK 介面

### 為何不用 requests

- httpx 原生支援 async（之後可能需要）
- SSE streaming 處理更好
- API 幾乎相同，無學習成本

## 檔案結構

```
chat-agent/
├── .env.example                      # 環境變數範本
├── cfgs/
│   ├── basic.yaml                    # 主配置
│   └── llm/
│       ├── ollama/
│       │   └── default.yaml
│       ├── anthropic/
│       │   └── default.yaml
│       ├── openai/
│       │   └── default.yaml
│       └── gemini/
│           └── default.yaml
│
└── src/lincy/
    ├── __init__.py
    ├── cli.py                        # CLI 入口
    │
    ├── core/
    │   ├── __init__.py
    │   └── config.py                 # Config 載入
    │
    ├── llm/
    │   ├── __init__.py
    │   ├── base.py                   # Message, LLMClient Protocol
    │   ├── factory.py                # create_client(config)
    │   └── providers/
    │       ├── __init__.py
    │       ├── ollama.py
    │       ├── anthropic.py
    │       ├── openai.py
    │       └── gemini.py
    │
    └── context/
        ├── __init__.py
        ├── conversation.py           # Conversation 對話歷史
        └── builder.py                # ContextBuilder 上下文組裝
```

## Config 系統設計

### basic.yaml 格式

```yaml
agents:
  brain:                              # 主 agent（大腦）
    llm: llm/ollama/default.yaml

  # 之後擴充
  # context_compressor:               # 壓縮上下文
  #   llm: llm/ollama/small.yaml
  # tool_user:                        # 工具使用
  #   llm: llm/anthropic/default.yaml
```

- 支援多 agent（brain、context_compressor、tool_user 等）
- 每個 agent 可指定不同的 LLM config
- `llm:` 值是相對於 `cfgs/` 的路徑，由 `config.py` 解析並載入

### LLM config 範例

```yaml
# cfgs/llm/ollama/default.yaml
provider: ollama
model: llama3.2
base_url: http://localhost:11434
```

```yaml
# cfgs/llm/anthropic/default.yaml
provider: anthropic
model: claude-sonnet-4-6
api_key_env: ANTHROPIC_API_KEY    # 從環境變數讀取
max_tokens: 4096
```

### config.py 職責

- 載入 yaml 檔案
- 解析路徑引用（`llm: llm/xxx.yaml` → 讀取並合併內容）
- 從環境變數讀取 API key

## LLM 抽象層設計

### base.py

```python
@dataclass
class Message:
    role: str       # "user" | "assistant" | "system"
    content: str

class LLMClient(Protocol):
    def chat(self, messages: list[Message]) -> str: ...
```

### providers/ 子資料夾

- 每個 provider 一個檔案
- 都實作 `LLMClient` Protocol
- 之後 provider 變多時結構清晰

### factory.py

```python
def create_client(config: dict) -> LLMClient:
    """根據 config 的 provider 欄位建立對應 client"""
    ...
```

## Context 管理設計

### 為何分離 Conversation 和 ContextBuilder

- **Conversation**：只負責儲存對話歷史
- **ContextBuilder**：負責組裝送給 LLM 的上下文

分離原因：之後要做上下文工程，組裝邏輯會很複雜（動態載入記憶、壓縮歷史、注入 system prompt 等），
不應該跟單純的歷史儲存混在一起。

### Conversation

```python
class Conversation:
    messages: list[Message]

    def add(self, role: str, content: str): ...
    def get_messages(self) -> list[Message]: ...
```

### ContextBuilder

```python
class ContextBuilder:
    """組裝要送給 LLM 的上下文"""

    def build(self, conversation: Conversation, **kwargs) -> list[Message]:
        """
        組裝來源（預留擴充）：
        - system prompt（可能動態生成）
        - 載入的記憶
        - 對話歷史（可能壓縮/摘要）
        - 當前用戶輸入
        - 其他注入的 context
        """
        ...
```

## 步驟

1. 更新 `pyproject.toml` 加入依賴（httpx、pyyaml、python-dotenv）
2. 建立 `.env.example` 和更新 `.gitignore`
3. 建立 `cfgs/basic.yaml` 和 `cfgs/llm/ollama/default.yaml`
4. 建立 `core/config.py`
5. 建立 `llm/base.py`
6. 建立 `llm/providers/ollama.py`
7. 建立 `llm/factory.py`
8. 建立 `context/conversation.py`
9. 建立 `context/builder.py`
10. 建立 `cli.py` 整合測試

## 驗證

```bash
# 確保 Ollama 運行中
ollama serve

# 執行 CLI
uv run python -m lincy

# 預期：能輸入訊息、收到 LLM 回應、多輪對話保持上下文
```

## 完成條件

- [x] Config 系統能載入 yaml 並解析路徑引用
- [x] LLM client 能呼叫 Ollama API
- [x] Conversation 能儲存對話歷史
- [x] ContextBuilder 能組裝上下文
- [x] CLI 能進行多輪對話
- [x] 切換 `basic.yaml` 的 llm 設定後，能使用不同 provider

## 已實作 Providers

| Provider | 配置路徑 | 測試狀態 |
|----------|----------|----------|
| Ollama | `llm/ollama/default.yaml` | ✅ |
| OpenAI | `llm/openai/default.yaml` | ✅ |
| Anthropic | `llm/anthropic/default.yaml` | ✅ |
| Gemini | `llm/gemini/default.yaml` | ✅ |
