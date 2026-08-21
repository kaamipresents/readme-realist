# System Data Flow & Architectural Blueprint

## 1. Product & Architecture Overview
**ReadMe Realist** is an automated CI gatekeeper and GitHub App that detects documentation drift during pull requests. It receives webhook events, extracts structural code delta signals (e.g., modified CLI flags, environment variables, dependencies, and entry points) via a local parser, evaluates the change against existing repository documentation via LLM backends (such as Google Gemini), and orchestrates feedback directly into GitHub Check Runs and PR Comments.

---

## 2. End-to-End Visual Dataflow

```mermaid
flowchart TD
    %% Styling Classes
    classDef client fill:#2c3e50,stroke:#34495e,stroke-width:2px,color:#fff;
    classDef gateway fill:#2980b9,stroke:#1f618d,stroke-width:2px,color:#fff;
    classDef worker fill:#8e44ad,stroke:#71368a,stroke-width:2px,color:#fff;
    classDef service fill:#d35400,stroke:#ba4a00,stroke-width:2px,color:#fff;
    classDef external fill:#27ae60,stroke:#1e8449,stroke-width:2px,color:#fff;
    classDef error fill:#c0392b,stroke:#962d22,stroke-width:2px,color:#fff;

    subgraph GitHub_Event ["GitHub Platform & Client Trigger"]
        A([Developer opens / updates PR]) -->|Webhook Event Delivery| B[GitHub Event Dispatcher]
    end

    subgraph API_Gateway ["Gateway & Signature Verification"]
        B -->|POST /webhooks/github + X-Hub-Signature-256| C[Webhook Route Handler]
        C --> D{Verify HMAC Signature}
        D -->|Invalid Signature| E[HTTP 401 Unauthorized]
        D -->|Ping Event| F[HTTP 200 Pong]
        D -->|Non-PR Event| G[HTTP 200 Ignored]
        D -->|Valid PR Event| H[Parse Pull Request Payload]
        H -->|Draft / Unsupported Action| I[HTTP 200 Ignored]
        H -->|Valid PR Context| J[Background Worker Queue]
        J -->|HTTP 202 Accepted| B
    end

    subgraph Pipeline_Processing ["Review Pipeline Orchestration"]
        J -->|Dispatch Async Task| K[ReviewPipeline Orchestrator]
        K --> L[Start Check Run in_progress]
        L --> M[Fetch PR Unified Diff]
        M --> N[Code Delta Parser & Signal Extractor]
        N --> O{Skip Decision Engine}
        O -->|No Files / Noise-only / Docs-only| P[Publish Skipped Status]
        O -->|Substantive Code Changes| Q[Fetch Documentation Files]
        Q --> R{Docs Exist?}
        R -->|No Matching Docs| S[Publish Skipped / Neutral Check]
        R -->|Docs Retrieved| T[LLM Semantic Evaluator]
    end

    subgraph LLM_Service ["LLM Evaluation Layer"]
        T -->|Prompt: Diff + Signals + Docs| U[Google Gemini / LLM Backend]
        U -->|Structured Verdict JSON| V{Drift Detected?}
    end

    subgraph Feedback_Publishing ["Feedback Orchestrator & Persistence"]
        V -->|UP_TO_DATE| W[Mark Check Run Success & Upsert Resolved Comment]
        V -->|NEEDS_UPDATE| X[Post/Update PR Comment with Suggested Edits & Check Run]
        P --> Y[Finalize Check Run completed]
        S --> Y
        W --> Y
        X --> Y
        E --> Z([End Lifecycle])
        F --> Z
        G --> Z
        I --> Z
        Y --> Z
    end

    class A,B client;
    class C,D,H,J gateway;
    class K,L,M,N,O,Q,R,T worker;
    class U service;
    class W,X,P,S,Y external;
    class E,F,G,I error;
```

---

## 3. Data Transformation & Step Lifecycle

| Step ID | Source Node | Target Node | Data Contract / Payload | Transformation / Business Logic |
| :--- | :--- | :--- | :--- | :--- |
| **01** | GitHub PR Event | Webhook Receiver | Raw JSON Payload + `X-Hub-Signature-256` | Delivers webhook delivery with HMAC-SHA256 signature headers. |
| **02** | Webhook Receiver | Signature Validator | Raw Bytes + Secret Key | Validates cryptographic signature before parsing payload to prevent tampering. |
| **03** | Webhook Receiver | Background Worker | `PullRequestContext` Object | Sanitizes payload into domain model and enqueues review task asynchronously. |
| **04** | Pipeline Orchestrator | GitHub REST API | Authenticated Installation JWT | Creates initial GitHub Check Run in `in_progress` state and downloads PR diff. |
| **05** | Diff Downloader | Code Delta Parser | Unified Diff String | Filters out binary/lockfile noise and extracts structural signals (CLI, env, deps, routes). |
| **06** | Delta Parser | Doc Fetcher / GitHub | File Globs (`README.md`, `docs/**`) | Scans repo for target documentation files matching configured glob patterns. |
| **07** | Pipeline Orchestrator | LLM Evaluator | `DiffAnalysis` + `DocumentationBundle` | Formats context and prompt instructions requesting structured `DriftVerdict` JSON. |
| **08** | LLM Backend | Feedback Orchestrator | `DriftVerdict` (`status`, `reason`, `suggested_edit`) | Parses model response into verdict structure with proposed markdown patches. |
| **09** | Feedback Orchestrator | GitHub PR & Checks | Markdown Comment + Check Run Update | Upserts single persistent PR review comment (via marker tag) and marks check completion. |

---

## 4. Guardrails & Failure Modes

* **Early-Exit Skip Engine:** PRs with no files, formatting/whitespace-only changes, or doc-only edits skip LLM inference, reducing latency and cost.
* **Fail-Open Check Conclusion:** Review pipeline errors or token failures publish neutral checks without blocking developer pull requests.
* **Idempotent PR Comments:** Feedback comments are tagged with an invisible comment marker (`<!-- readme-realist:v1 -->`) and upserted in place to prevent comment spamming across repeated pushes.
