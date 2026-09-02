import { inr } from "@/lib/format";
import { LAMP, type StateKey } from "@/lib/states";

/** The state lamp - a small dot in one of the five mandated colors. The running
 *  state pulses unless the user prefers reduced motion (handled in CSS). */
export function Lamp({
  state,
  pulse = false,
  size = 8,
}: {
  state: StateKey;
  pulse?: boolean;
  size?: number;
}) {
  return (
    <span
      aria-hidden
      className={`inline-block shrink-0 rounded-full ${LAMP[state].dot} ${
        pulse && state === "running" ? "lamp-pulse" : ""
      }`}
      style={{ width: size, height: size }}
    />
  );
}

/** A small status chip: wash background + colored text + hairline border. */
export function StateChip({
  state,
  children,
}: {
  state: StateKey;
  children: React.ReactNode;
}) {
  return (
    <span
      className={`inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[11px] font-medium ${LAMP[state].chip}`}
    >
      {children}
    </span>
  );
}

/** A rupee figure in tabular mono, so animated/among-siblings values don't shift. */
export function Amount({
  paise,
  paiseMode = false,
  className = "",
}: {
  paise: number | null | undefined;
  paiseMode?: boolean;
  className?: string;
}) {
  return (
    <span className={`font-mono tabular-nums ${className}`}>
      {inr(paise, { paise: paiseMode })}
    </span>
  );
}

type Material = "dark" | "paper";

/** Panel wrapper that adapts to whichever material it sits on. */
export function Card({
  material = "paper",
  title,
  right,
  children,
  className = "",
  bodyClassName = "",
}: {
  material?: Material;
  title?: React.ReactNode;
  right?: React.ReactNode;
  children: React.ReactNode;
  className?: string;
  bodyClassName?: string;
}) {
  const dark = material === "dark";
  return (
    <section
      className={`rounded border ${
        dark
          ? "border-rule bg-ink-raised"
          : "border-paper-rule bg-paper-raised shadow-panel"
      } ${className}`}
    >
      {title && (
        <div
          className={`flex items-center justify-between gap-2 border-b px-4 py-2.5 ${
            dark ? "border-rule" : "border-paper-rule"
          }`}
        >
          <h2
            className={`text-[11px] font-semibold uppercase tracking-[0.16em] ${
              dark ? "text-ink-dim" : "text-paper-dim"
            }`}
          >
            {title}
          </h2>
          {right}
        </div>
      )}
      <div className={`px-4 py-3 ${bodyClassName}`}>{children}</div>
    </section>
  );
}

/** A quiet empty-state line, tuned to the material. */
export function Empty({
  material = "paper",
  children,
}: {
  material?: Material;
  children: React.ReactNode;
}) {
  const dark = material === "dark";
  return (
    <p
      className={`py-6 text-center text-xs ${dark ? "text-ink-faint" : "text-paper-faint"}`}
    >
      {children}
    </p>
  );
}
