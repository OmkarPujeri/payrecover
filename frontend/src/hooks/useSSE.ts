"use client";

/**
 * One EventSource, managed correctly. Everything tricky about SSE in React lives
 * here so the provider stays declarative:
 *
 *   - Callbacks are stashed in refs, so the connection is opened once per URL and
 *     survives parent re-renders instead of tearing down on every render.
 *   - StrictMode double-mounts in dev: the effect's cleanup closes the socket, and
 *     the re-run opens a fresh one - no leaked connections.
 *   - The browser's EventSource reconnects on its own after a drop; we surface
 *     that as `"reconnecting"` (vs `"closed"` when it has truly given up) so the
 *     provider can trigger a REST resync on the next open (TRAP #4).
 *   - Keep-alive comment frames (`: keep-alive`) never reach `onmessage` - the
 *     browser strips comment lines - so there's nothing to filter here.
 */

import { useEffect, useRef } from "react";
import { parseFrame } from "@/lib/adapters";
import type { ConnStatus } from "@/lib/store";
import type { SSEFrame } from "@/lib/types";

interface UseSSEArgs {
  url: string;
  onFrame: (frame: SSEFrame) => void;
  onStatus: (status: ConnStatus) => void;
  enabled?: boolean;
}

export function useSSE({ url, onFrame, onStatus, enabled = true }: UseSSEArgs): void {
  const onFrameRef = useRef(onFrame);
  const onStatusRef = useRef(onStatus);
  onFrameRef.current = onFrame;
  onStatusRef.current = onStatus;

  useEffect(() => {
    if (!enabled || typeof window === "undefined") return;

    onStatusRef.current("connecting");
    const es = new EventSource(url);

    es.onopen = () => onStatusRef.current("open");

    es.onmessage = (evt) => {
      const frame = parseFrame(evt.data);
      if (frame) onFrameRef.current(frame);
    };

    es.onerror = () => {
      // readyState: CONNECTING(0) means it's retrying; CLOSED(2) means it stopped.
      onStatusRef.current(
        es.readyState === EventSource.CLOSED ? "closed" : "reconnecting",
      );
    };

    return () => {
      es.close();
      onStatusRef.current("closed");
    };
  }, [url, enabled]);
}
