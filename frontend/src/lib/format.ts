/**
 * Formatting primitives. Two of these encode invariants the whole product
 * depends on, so they live in one file and nothing else reimplements them.
 *
 * `parseInstant` - trap #1 from the plan. Datetimes read back from a DB column
 * serialize *with* a `+00:00` offset on Postgres and with *no offset* on SQLite
 * (both go through Python's `.isoformat()`; SQLite simply discards the tzinfo on
 * write). `new Date("2026-08-23T04:47:01")` is parsed by JS as **local** time,
 * so on an IST machine an offset-less UTC value renders 5.5 hours off. This is
 * the exact skew that has already bitten the scheduler once. Every server
 * timestamp enters the client through here, and nothing else calls `new Date()`
 * on a server string.
 *
 * `inr` - money is paise everywhere in state (trap-adjacent invariant from the
 * plan); rupees exist only at render. Lakh-style grouping via `en-IN`, matching
 * the PRD's `₹3,85,200` throughout.
 */

const HAS_TZ = /(?:Z|[+-]\d{2}:?\d{2})$/;

/**
 * Parse a server datetime string into a `Date`, treating an offset-less value as
 * UTC rather than local. Returns `null` for null/empty/unparseable input so
 * callers can render a placeholder instead of `Invalid Date`.
 */
export function parseInstant(value: string | null | undefined): Date | null {
  if (!value) return null;
  const normalized = HAS_TZ.test(value) ? value : `${value}Z`;
  const d = new Date(normalized);
  return Number.isNaN(d.getTime()) ? null : d;
}

const INR_WHOLE = new Intl.NumberFormat("en-IN", {
  style: "currency",
  currency: "INR",
  maximumFractionDigits: 0,
});

const INR_PAISE = new Intl.NumberFormat("en-IN", {
  style: "currency",
  currency: "INR",
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

/**
 * Render a paise amount as rupees. Whole rupees by default (`₹3,85,200`); pass
 * `{ paise: true }` when the sub-rupee part matters (a single cost line, not a
 * hero total). `null`/`undefined` renders as `₹0`.
 */
export function inr(
  paise: number | null | undefined,
  opts: { paise?: boolean } = {},
): string {
  const rupees = (paise ?? 0) / 100;
  return opts.paise ? INR_PAISE.format(rupees) : INR_WHOLE.format(rupees);
}

const IST_TIME = new Intl.DateTimeFormat("en-IN", {
  hour: "2-digit",
  minute: "2-digit",
  hour12: true,
  timeZone: "Asia/Kolkata",
});

const IST_DATETIME = new Intl.DateTimeFormat("en-IN", {
  day: "2-digit",
  month: "short",
  hour: "2-digit",
  minute: "2-digit",
  hour12: true,
  timeZone: "Asia/Kolkata",
});

/** A server timestamp as IST wall-clock time, e.g. `07:30 AM`. */
export function ist(value: string | null | undefined): string {
  const d = parseInstant(value);
  return d ? IST_TIME.format(d) : "-";
}

/** A server timestamp as an IST date + time, e.g. `23 Aug, 07:30 AM`. */
export function istDateTime(value: string | null | undefined): string {
  const d = parseInstant(value);
  return d ? IST_DATETIME.format(d) : "-";
}

/** Fractional hours-of-day in IST (e.g. 19.5 for 7:30 PM) - used by the band. */
export function istHourOfDay(value: string | null | undefined): number | null {
  const d = parseInstant(value);
  if (!d) return null;
  // Read the wall-clock parts *in IST* rather than doing offset math, so DST-free
  // IST (+5:30, always) is handled by the formatter, not by us.
  const parts = IST_TIME_PARTS.formatToParts(d);
  const h = Number(parts.find((p) => p.type === "hour")?.value ?? NaN);
  const m = Number(parts.find((p) => p.type === "minute")?.value ?? NaN);
  if (Number.isNaN(h) || Number.isNaN(m)) return null;
  return h + m / 60;
}

const IST_TIME_PARTS = new Intl.DateTimeFormat("en-GB", {
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
  timeZone: "Asia/Kolkata",
});

/** Compact relative time from now, e.g. `just now`, `3m ago`, `2h ago`. */
export function relativeTime(value: string | null | undefined): string {
  const d = parseInstant(value);
  if (!d) return "-";
  const secs = Math.round((Date.now() - d.getTime()) / 1000);
  if (secs < 5) return "just now";
  if (secs < 60) return `${secs}s ago`;
  const mins = Math.round(secs / 60);
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.round(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.round(hours / 24);
  return `${days}d ago`;
}

/** Short id for display - `pay_sim_a1b2c3d4` → `a1b2c3d4`, uuid → first 8. */
export function shortId(id: string | null | undefined): string {
  if (!id) return "-";
  const tail = id.includes("_") ? id.split("_").pop()! : id;
  return tail.slice(0, 8);
}

/** Percent with one decimal, e.g. `72.5%`. */
export function pct(value: number | null | undefined): string {
  return `${(value ?? 0).toFixed(1)}%`;
}
