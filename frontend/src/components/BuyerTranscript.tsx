import type { TranscriptEntry } from "../api";
import { cx } from "../ui";

export function BuyerTranscript({ entries }: { entries: TranscriptEntry[] }) {
  return (
    <div className="tscript">
      {entries.map((t, i) => (
        <div
          key={i}
          className={cx(
            "tline",
            t.kind === "model_text" && "tline--model",
            t.kind === "tool_call" && "tline--call",
            t.kind === "tool_result" && "tline--result",
          )}
          style={{ animationDelay: `${Math.min(i, 20) * 0.03}s` }}
        >
          <div className="tline__step">{t.step}</div>
          <div className="tline__body">
            {t.kind === "model_text" && <span>{String(t.detail)}</span>}
            {t.kind === "tool_call" && (
              <>
                <span className="tline__tool">→ {t.tool}(</span>
                <pre className="tline__json">{compact(t.detail)}</pre>
                <span className="tline__tool">)</span>
              </>
            )}
            {t.kind === "tool_result" && (
              <>
                <span className="tline__tool">← {t.tool}</span>
                <pre className="tline__json">{compact(t.detail)}</pre>
              </>
            )}
          </div>
        </div>
      ))}
    </div>
  );
}

function compact(v: unknown): string {
  try {
    const s = JSON.stringify(v, null, 2);
    return s.length > 1400 ? s.slice(0, 1400) + "\n…" : s;
  } catch {
    return String(v);
  }
}
