import React, { useState, useRef, useEffect, useCallback } from "react";
import { GhostPreviewModal } from "@/components/ghost/GhostPreviewModal";
import type { ChatMessage, GhostAction } from "@/types";
import { Zap, MessageSquare } from "lucide-react";
interface Props {
  onOpenGhost?: (action: GhostAction) => void;
}

const QUICK_PROMPTS = [
  "Explain the root cause of INC-2847",
  "What is the blast radius of the current attack?",
  "Propose safe remediation for postgres OOM",
  "Compare risk: isolate vs restart for order-svc",
  "Generate incident report for INC-2847",
];

// ── The API base URL — empty string means same origin (proxied by Vite/nginx)
const API_BASE = "";

function detectProposedAction(text: string): GhostAction | null {
  const match = text.match(/PROPOSED ACTION:\s*(\w+)\s+on\s+([\w-]+)/i);
  if (!match) return null;
  const actionMap: Record<string, string> = {
    isolate_container: "isolate_container",
    increase_memory_limit: "increase_memory_limit",
    restart_pod: "restart_pod",
    apply_network_policy: "apply_network_policy",
    block_ip: "block_ip",
    scale_up: "scale_up",
    scale_down: "scale_down",
    rollback: "rollback",
  };
  const actionName = actionMap[match[1]] ?? match[1];
  return {
    label: `${match[1]} on ${match[2]}`,
    icon: <Zap size={14} color="#FF8C00" />,
    actionName,
    targetNode: match[2],
  };
}

// ── Simple markdown renderer ──────────────────────────────────────────────────
function renderMarkdown(text: string) {
  const lines = text.split("\n");
  return lines.map((line, i) => {
    // Full-line bold heading
    if (/^\*\*[^*]+\*\*$/.test(line.trim())) {
      return (
        <div
          key={i}
          style={{
            fontWeight: 700,
            color: "#C8D8E8",
            marginTop: 8,
            marginBottom: 2,
          }}
        >
          {line.replace(/\*\*/g, "")}
        </div>
      );
    }
    // Line with inline bold
    if (line.includes("**")) {
      const parts = line.split(/\*\*(.*?)\*\*/g);
      return (
        <div key={i} style={{ lineHeight: 1.6, marginBottom: 2 }}>
          {parts.map((p, j) =>
            j % 2 === 1 ? (
              <strong key={j} style={{ color: "#00D4FF" }}>
                {p}
              </strong>
            ) : (
              p
            ),
          )}
        </div>
      );
    }
    // Bullet / numbered list item
    if (line.startsWith("- ") || /^\d+\./.test(line)) {
      return (
        <div
          key={i}
          style={{ paddingLeft: 16, lineHeight: 1.6, marginBottom: 1 }}
        >
          {line}
        </div>
      );
    }
    // PROPOSED ACTION highlight
    if (/^PROPOSED ACTION:/i.test(line)) {
      return (
        <div
          key={i}
          style={{
            marginTop: 8,
            padding: "6px 10px",
            background: "#9B5DE522",
            border: "1px solid #9B5DE544",
            borderRadius: 6,
            color: "#9B5DE5",
            fontSize: 12,
            fontFamily: "JetBrains Mono, monospace",
          }}
        >
          {line}
        </div>
      );
    }
    // Empty line
    if (line.trim() === "") return <div key={i} style={{ height: 6 }} />;
    // Default
    return (
      <div key={i} style={{ lineHeight: 1.6, marginBottom: 2 }}>
        {line}
      </div>
    );
  });
}

// ── Main component ────────────────────────────────────────────────────────────
export const CopilotTab: React.FC<Props> = () => {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [streaming, setStreaming] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [ghostAction, setGhostAction] = useState<GhostAction | null>(null);

  const bottomRef = useRef<HTMLDivElement>(null);
  const abortRef = useRef<AbortController | null>(null);
  const sessionId = useRef(`session-${Date.now()}`);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  const send = useCallback(
    async (text: string) => {
      if (!text.trim() || streaming) return;

      setError(null);
      setGhostAction(null);

      const userMsg: ChatMessage = {
        role: "user",
        content: text.trim(),
        ts: Date.now(),
      };
      setMessages((prev) => [...prev, userMsg]);
      setInput("");
      setStreaming(true);

      // Create a new assistant message placeholder
      const assistantPlaceholder: ChatMessage = {
        role: "assistant",
        content: "",
        ts: Date.now(),
      };
      setMessages((prev) => [...prev, assistantPlaceholder]);

      const controller = new AbortController();
      abortRef.current = controller;

      let accumulated = "";

      // Retry logic with exponential backoff for 429 errors
      const maxRetries = 3;
      let retryCount = 0;
      let resp: Response | null = null;

      while (retryCount <= maxRetries) {
        try {
          resp = await fetch(`${API_BASE}/api/v1/copilot/chat`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              session_id: sessionId.current,
              message: text.trim(),
              stream: true,
              context: {},
            }),
            signal: controller.signal,
          });

          if (resp.ok) {
            break; // Success - exit retry loop
          }

          // Handle 429 rate limit with retry
          if (resp.status === 429) {
            const errJson = await resp.json().catch(() => ({}));
            const retryAfter = errJson.retry_after_seconds || Math.pow(2, retryCount);

            if (retryCount < maxRetries) {
              console.log(`Rate limited. Retrying in ${retryAfter}s... (attempt ${retryCount + 1}/${maxRetries})`);
              accumulated = `⏳ Rate limit reached. Retrying in ${Math.ceil(retryAfter)}s...\n`;

              setMessages((prev) => {
                const updated = [...prev];
                const last = updated[updated.length - 1];
                if (last?.role === "assistant") {
                  updated[updated.length - 1] = { ...last, content: accumulated };
                }
                return updated;
              });

              await new Promise(resolve => setTimeout(resolve, retryAfter * 1000));
              retryCount++;
              continue;
            }
          }

          // Other errors - show message and exit
          const errText = await resp.text();
          console.warn("API error:", resp.status, errText);
          accumulated += `\n\n⚠️ Service temporarily unavailable (${resp.status}). Please try again.\n`;

          setMessages((prev) => {
            const updated = [...prev];
            const last = updated[updated.length - 1];
            if (last?.role === "assistant") {
              updated[updated.length - 1] = { ...last, content: accumulated };
            }
            return updated;
          });

          setStreaming(false);
          return;

        } catch (fetchErr) {
          if (retryCount < maxRetries) {
            retryCount++;
            await new Promise(resolve => setTimeout(resolve, Math.pow(2, retryCount) * 1000));
            continue;
          }
          throw fetchErr;
        }
      }

      if (!resp || !resp.ok) {
        accumulated += `\n\n⚠️ Unable to reach Co-Pilot after ${maxRetries} retries.\n`;
        setMessages((prev) => {
          const updated = [...prev];
          const last = updated[updated.length - 1];
          if (last?.role === "assistant") {
            updated[updated.length - 1] = { ...last, content: accumulated };
          }
          return updated;
        });
        setStreaming(false);
        return;
      }

      try {

        const reader = resp.body!.getReader();
        const decoder = new TextDecoder();
        let buffer = "";

        while (true) {
          const { done, value } = await reader.read();
          if (done) break;

          buffer += decoder.decode(value, { stream: true });
          const lines = buffer.split("\n");
          buffer = lines.pop() ?? ""; // keep incomplete last line in buffer

          for (const line of lines) {
            if (!line.startsWith("data: ")) continue;
            const raw = line.slice(6).trim();
            if (!raw) continue;

            let payload: any;
            try {
              payload = JSON.parse(raw);
            } catch {
              continue;
            }

            if (payload.type === "text_delta") {
              accumulated += payload.text ?? "";
              setMessages((prev) => {
                const updated = [...prev];
                const last = updated[updated.length - 1];
                if (last?.role === "assistant") {
                  updated[updated.length - 1] = {
                    ...last,
                    content: accumulated,
                  };
                }
                return updated;
              });
            } else if (payload.type === "tool_call") {
              // Tool calls are informational — optionally show them
              console.info("[tool_call]", payload.tool);
            } else if (payload.type === "error") {
              console.warn("LLM error:", payload.message);

              accumulated +=
                "\n\n⚠️ Temporary AI issue. Retrying might help.\n";

              setMessages((prev) => {
                const updated = [...prev];
                const last = updated[updated.length - 1];
                if (last?.role === "assistant") {
                  updated[updated.length - 1] = {
                    ...last,
                    content: accumulated,
                  };
                }
                return updated;
              });

              continue; // IMPORTANT: do NOT throw
            } else if (payload.type === "done") {
              break;
            }
          }
        }

        // Detect proposed action in the final reply
        if (accumulated) {
          const ga = detectProposedAction(accumulated);
          if (ga) setGhostAction(ga);
        }
      } catch (err: any) {
        if (err.name === "AbortError") return; // user cancelled
        const errMsg = err.message ?? "Connection failed";
        setError(errMsg);
        // Replace placeholder with error message
        setMessages((prev) => {
          const updated = [...prev];
          const last = updated[updated.length - 1];
          if (last?.role === "assistant" && last.content === "") {
            updated[updated.length - 1] = {
              ...last,
              content: `⚠️ Error: ${errMsg}`,
            };
          }
          return updated;
        });
      } finally {
        setStreaming(false);
        abortRef.current = null;
      }
    },
    [streaming],
  );

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      send(input);
    }
  };

  const handleStop = () => {
    abortRef.current?.abort();
    setStreaming(false);
  };

  return (
    <div
      style={{
        height: "100%",
        display: "flex",
        flexDirection: "column",
        overflow: "hidden",
      }}
    >
      {/* Context banner */}
      <div
        style={{
          padding: "8px 20px",
          borderBottom: "1px solid #0D2244",
          background: "#040C1A",
          display: "flex",
          alignItems: "center",
          flexShrink: 0,
          flexWrap: "wrap",
          gap: 12,
        }}
      >
        <span
          style={{
            fontSize: 10,
            color: "#4A6A8A",
            textTransform: "uppercase",
            letterSpacing: 0.5,
          }}
        >
          Context:
        </span>
        {[
          { label: "Root Cause: order-svc", color: "#FF3B5C" },
          { label: "Confidence: 94.2%", color: "#00D4FF" },
          { label: "2 Critical Alerts", color: "#FF3B5C" },
          { label: "OPA: 100%", color: "#00FF9F" },
          { label: "Co-Pilot", color: "#9B5DE5" },
        ].map((c) => (
          <span
            key={c.label}
            style={{
              fontSize: 10,
              color: c.color,
              fontFamily: "JetBrains Mono, monospace",
              background: `${c.color}15`,
              border: `1px solid ${c.color}33`,
              borderRadius: 4,
              padding: "1px 6px",
            }}
          >
            {c.label}
          </span>
        ))}
      </div>

      {/* Messages */}
      <div style={{ flex: 1, overflowY: "auto", padding: "16px 20px" }}>
        {/* Empty state */}
        {messages.length === 0 && (
          <div style={{ textAlign: "center", marginTop: 40, color: "#4A6A8A" }}>
            <div
              style={{
                display: "flex",
                justifyContent: "center",
                marginBottom: 12,
              }}
            >
              <MessageSquare size={36} color="#FFD60A" />
            </div>
            <div style={{ fontSize: 14, marginBottom: 6, color: "#f0eff1ff" }}>
              CCDT Co-Pilot
            </div>
            <div style={{ fontSize: 12, color: "#f3f3f4ff" }}>
              Ask about incidents, root causes, or request remediation proposals
            </div>
            <div
              style={{
                marginTop: 20,
                display: "flex",
                flexWrap: "wrap",
                gap: 8,
                justifyContent: "center",
              }}
            >
              {QUICK_PROMPTS.map((p) => (
                <button
                  key={p}
                  onClick={() => send(p)}
                  style={{
                    background: "#06111F",
                    border: "1px solid #0D2244",
                    borderRadius: 6,
                    color: "#cdb8ffff",
                    fontSize: 11,
                    padding: "5px 10px",
                    cursor: "pointer",
                    maxWidth: 200,
                    textAlign: "center",
                  }}
                  onMouseEnter={(e) =>
                    (e.currentTarget.style.borderColor = "#00D4FF44")
                  }
                  onMouseLeave={(e) =>
                    (e.currentTarget.style.borderColor = "#0D2244")
                  }
                >
                  {p}
                </button>
              ))}
            </div>
          </div>
        )}

        {/* Message list */}
        {messages.map((msg, i) => (
          <div
            key={i}
            style={{
              display: "flex",
              justifyContent: msg.role === "user" ? "flex-end" : "flex-start",
              marginBottom: 14,
            }}
          >
            {msg.role === "assistant" && (
              <div
                style={{
                  width: 28,
                  height: 28,
                  borderRadius: "50%",
                  background: "#9B5DE522",
                  border: "1px solid #9B5DE544",
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                  fontSize: 14,
                  marginRight: 10,
                  flexShrink: 0,
                  marginTop: 2,
                }}
              >
                🧠
              </div>
            )}

            <div
              style={{
                maxWidth: "75%",
                padding: "12px 14px",
                borderRadius:
                  msg.role === "user"
                    ? "12px 12px 4px 12px"
                    : "4px 12px 12px 12px",
                background: msg.role === "user" ? "#00D4FF22" : "#06111F",
                border: `1px solid ${msg.role === "user" ? "#00D4FF44" : "#0D2244"}`,
                fontSize: 12,
                color: "#C8D8E8",
                lineHeight: 1.6,
              }}
            >
              {msg.role === "assistant"
                ? renderMarkdown(msg.content)
                : msg.content}

              {/* Streaming cursor */}
              {streaming &&
                msg.role === "assistant" &&
                i === messages.length - 1 && (
                  <span
                    style={{
                      display: "inline-block",
                      width: 8,
                      height: 14,
                      background: "#9B5DE5",
                      marginLeft: 2,
                      verticalAlign: "text-bottom",
                      animation: "blink 0.8s infinite",
                    }}
                  />
                )}
            </div>
          </div>
        ))}

        {/* Ghost Preview banner */}
        {ghostAction && !streaming && (
          <div
            style={{
              margin: "8px 0",
              padding: "10px 14px",
              background: "#9B5DE511",
              border: "1px solid #9B5DE544",
              borderRadius: 8,
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
            }}
          >
            <div
              style={{
                display: "flex",
                alignItems: "center",
                gap: 6,
                fontSize: 12,
                color: "#9B5DE5",
              }}
            >
              <Zap size={14} color="#FF8C00" />
              <span>
                Proposed action detected: <strong>{ghostAction.label}</strong>
              </span>
            </div>
            <button
              onClick={() => setGhostAction(ghostAction)}
              style={{
                background: "#9B5DE5",
                border: "none",
                borderRadius: 6,
                color: "#030810",
                fontSize: 11,
                fontWeight: 700,
                padding: "5px 12px",
                cursor: "pointer",
              }}
            >
              👻 Ghost Preview
            </button>
          </div>
        )}

        {/* Error banner */}
        {error && (
          <div
            style={{
              margin: "8px 0",
              padding: "8px 14px",
              background: "#FF3B5C11",
              border: "1px solid #FF3B5C44",
              borderRadius: 8,
              fontSize: 12,
              color: "#FF3B5C",
            }}
          >
            ⚠️ {error}
          </div>
        )}

        <div ref={bottomRef} />
      </div>

      {/* Quick prompts (shown after first message) */}
      {messages.length > 0 && (
        <div
          style={{
            padding: "8px 20px",
            borderTop: "1px solid #0D2244",
            display: "flex",
            gap: 6,
            flexWrap: "wrap",
            flexShrink: 0,
          }}
        >
          {QUICK_PROMPTS.slice(0, 3).map((p) => (
            <button
              key={p}
              onClick={() => send(p)}
              disabled={streaming}
              style={{
                background: "transparent",
                border: "1px solid #0D2244",
                borderRadius: 5,
                color: "#4A6A8A",
                fontSize: 10,
                padding: "3px 8px",
                cursor: streaming ? "default" : "pointer",
                opacity: streaming ? 0.5 : 1,
              }}
            >
              {p}
            </button>
          ))}
        </div>
      )}

      {/* Input row */}
      <div
        style={{
          padding: "12px 20px",
          borderTop: "1px solid #0D2244",
          display: "flex",
          gap: 10,
          alignItems: "flex-end",
          flexShrink: 0,
          background: "#040C1A",
        }}
      >
        <textarea
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder="Ask CCDT Co-Pilot… (Enter to send, Shift+Enter for newline)"
          rows={2}
          style={{
            flex: 1,
            background: "#06111F",
            border: "1px solid #0D2244",
            borderRadius: 8,
            color: "#C8D8E8",
            fontSize: 13,
            padding: "10px 12px",
            resize: "none",
            outline: "none",
            fontFamily: "Inter, sans-serif",
            lineHeight: 1.5,
          }}
          onFocus={(e) => (e.currentTarget.style.borderColor = "#00D4FF44")}
          onBlur={(e) => (e.currentTarget.style.borderColor = "#0D2244")}
        />

        {streaming ? (
          <button
            onClick={handleStop}
            style={{
              background: "#FF3B5C",
              border: "none",
              borderRadius: 8,
              color: "#fff",
              fontSize: 13,
              fontWeight: 700,
              padding: "10px 18px",
              cursor: "pointer",
              height: 56,
              flexShrink: 0,
            }}
          >
            ■ Stop
          </button>
        ) : (
          <button
            onClick={() => send(input)}
            disabled={!input.trim()}
            style={{
              background: input.trim() ? "#00D4FF" : "#0D2244",
              border: "none",
              borderRadius: 8,
              color: input.trim() ? "#030810" : "#4A6A8A",
              fontSize: 13,
              fontWeight: 700,
              padding: "10px 18px",
              cursor: input.trim() ? "pointer" : "default",
              height: 56,
              flexShrink: 0,
              transition: "background 0.2s",
            }}
          >
            →
          </button>
        )}
      </div>

      {ghostAction && (
        <GhostPreviewModal
          action={ghostAction}
          onClose={() => setGhostAction(null)}
        />
      )}
    </div>
  );
};

export default CopilotTab;
