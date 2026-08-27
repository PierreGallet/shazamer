// Typed client for the Shazamer API.
//
// Every response shape the backend produces is declared here, so a field
// renamed in Python surfaces as a TypeScript error rather than as `undefined`
// rendering blank in the UI.

export interface Track {
  index: number;
  start: number;
  end: number;
  start_label: string;
  duration: number;
  identified: boolean;
  title: string;
  artist: string;
  url: string;
  cover_url: string;
  album: string;
  label: string;
  year: string;
  genre: string;
  isrc: string;
  /** Filled in after the analysis by MusicBrainz, when it can be found. */
  catalog_number: string;
  mbid: string;
  key: string;
  confidence: number;
  /** Share of the probes that named *something* which agreed. Silence is not
   *  dissent — fingerprinting fails on breakdowns and unreleased passages. */
  agreement: number;
  /** How much evidence backs the match, not just how much of it agreed. */
  strength: "strong" | "medium" | "weak" | "none" | "";
  votes: number;
  probes: number;
  bpm: number | null;
  camelot: string | null;
  musical_key: string | null;
  starred?: boolean;
}

export interface SetStats {
  probes?: number;
  probes_matched?: number;
  segments?: number;
  identified?: number;
  unidentified?: number;
  coverage?: number;
  elapsed_seconds?: number;
  /** Seconds per stage, in the order they ran. */
  stage_seconds?: Record<string, number>;
  strategy?: string;
  concurrency?: number;
}

export interface SetSummary {
  id: string;
  title: string;
  source_url: string;
  source_kind: string;
  uploader: string;
  quality: string;
  duration: number;
  has_audio: boolean;
  stats: SetStats;
  created_at: string;
  track_count: number | null;
  identified_count: number | null;
}

export interface SetDetail extends SetSummary {
  waveform: number[];
  tracks: Track[];
}

export interface TaskState {
  task_id: string;
  status: "pending" | "downloading" | "processing" | "completed" | "error" | "cancelled";
  progress: number;
  stage: string;
  message: string;
  filename: string;
  source_url: string;
  error: string | null;
  set_id: string | null;
  quality: string;
  created_at: string;
  finished_at: string | null;
  /** Estimated seconds remaining, from the observed rate. Null until there is
   *  enough movement to say anything honest. */
  eta_seconds: number | null;
}

export interface LibraryTrack {
  key: string;
  title: string;
  artist: string;
  url: string;
  cover_url: string;
  label: string;
  bpm: number | null;
  camelot: string | null;
  set_count: number;
  starred?: boolean;
  set_title?: string;
}

export interface AcquisitionSource {
  kind: "store" | "stream" | "p2p";
  name: string;
  url: string;
  quality: string;
  note: string;
  actionable: boolean;
}

export interface SoulseekCandidate {
  username: string;
  filename: string;
  full_path: string;
  size: number;
  extension: string;
  bitrate: number | null;
  /** Seconds. The field that separates an extended mix from a radio edit. */
  length: number | null;
  quality_label: string;
  duration_label: string;
  lossless: boolean;
  queue_length: number;
  free_slot: boolean;
  upload_speed: number;
  score: number;
}

export interface Download {
  id: number;
  track_key: string;
  artist: string;
  title: string;
  status: "queued" | "downloading" | "verifying" | "ready" | "failed";
  message: string;
  quality: string;
  username: string;
  filename: string;
  /** Whether the bytes are still on the server — they are swept after a while. */
  available: boolean;
  size: number;
  /** Fingerprinted and confirmed to be the track that was asked for. */
  verified: boolean;
  progress: number;
  created_at: string;
}

export interface Watch {
  id: string;
  url: string;
  title: string;
  kind: string;
  created_at: string;
  last_checked: string | null;
  seen_count: number;
}

export interface ChannelEntry {
  id: string;
  title: string;
  url: string;
  duration: number | null;
  uploader: string;
  thumbnail: string;
}

export class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, init);
  if (!response.ok) {
    let detail = `Request failed (${response.status})`;
    try {
      const body = await response.json();
      if (typeof body?.detail === "string") detail = body.detail;
    } catch {
      // Non-JSON error body — keep the status-based message.
    }
    throw new ApiError(detail, response.status);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export const api = {
  /**
   * Start an analysis from a URL.
   *
   * `replaces` re-analyses an existing set in place, which is how an imported
   * stub gets its audio, waveform, BPM and key — none of which were recorded
   * before 1.0, so running the source again is the only way to obtain them.
   */
  analyzeUrl: (url: string, replaces?: string) =>
    request<{ task_id: string; url: string; replaces: string | null }>(
      "/api/analyze/url",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url, replaces: replaces ?? null }),
      },
    ),

  analyzeUpload: (file: File, onProgress?: (pct: number) => void) =>
    new Promise<{ task_id: string; filename: string }>((resolve, reject) => {
      // XHR rather than fetch: it is still the only way to observe upload
      // progress, and a two-hour set is a large upload.
      const form = new FormData();
      form.append("file", file);
      const xhr = new XMLHttpRequest();
      xhr.open("POST", "/api/analyze/upload");
      xhr.upload.addEventListener("progress", (event) => {
        if (event.lengthComputable && onProgress) {
          onProgress((event.loaded / event.total) * 100);
        }
      });
      xhr.addEventListener("load", () => {
        if (xhr.status >= 200 && xhr.status < 300) {
          resolve(JSON.parse(xhr.responseText));
        } else {
          let detail = `Upload failed (${xhr.status})`;
          try {
            detail = JSON.parse(xhr.responseText).detail ?? detail;
          } catch {
            /* keep default */
          }
          reject(new ApiError(detail, xhr.status));
        }
      });
      xhr.addEventListener("error", () =>
        reject(new ApiError("Upload failed — the connection dropped.", 0)),
      );
      xhr.send(form);
    }),

  /** Analyses still running, for the header's "back to it" affordance. */
  activeTasks: () => request<TaskState[]>("/api/tasks"),
  task: (id: string) => request<TaskState>(`/api/tasks/${id}`),
  cancelTask: (id: string) =>
    request<{ cancelled: boolean }>(`/api/tasks/${id}/cancel`, { method: "POST" }),

  sets: (limit = 50) => request<SetSummary[]>(`/api/sets?limit=${limit}`),
  set: (id: string) => request<SetDetail>(`/api/sets/${id}`),
  deleteSet: (id: string) =>
    request<{ deleted: boolean }>(`/api/sets/${id}`, { method: "DELETE" }),
  audioUrl: (id: string) => `/api/sets/${id}/audio`,
  exportUrl: (id: string, fmt: string) => `/api/sets/${id}/export/${fmt}`,

  recurring: (minSets = 2) =>
    request<LibraryTrack[]>(`/api/library/recurring?min_sets=${minSets}`),
  searchLibrary: (params: {
    q?: string;
    bpmMin?: number;
    bpmMax?: number;
    camelot?: string;
    starred?: boolean;
  }) => {
    const query = new URLSearchParams();
    if (params.q) query.set("q", params.q);
    if (params.bpmMin != null) query.set("bpm_min", String(params.bpmMin));
    if (params.bpmMax != null) query.set("bpm_max", String(params.bpmMax));
    if (params.camelot) query.set("camelot", params.camelot);
    if (params.starred) query.set("starred", "true");
    return request<(Track & { set_title: string })[]>(
      `/api/library/search?${query.toString()}`,
    );
  },
  crate: () => request<LibraryTrack[]>("/api/library/crate"),
  star: (key: string, title: string, artist: string) =>
    request<{ key: string; starred: boolean }>("/api/library/star", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ key, title, artist }),
    }),

  acquisitionSources: (artist: string, title: string) => {
    const query = new URLSearchParams({ artist, title });
    return request<{ sources: AcquisitionSource[]; soulseek_configured: boolean }>(
      `/api/acquire/sources?${query.toString()}`,
    );
  },
  soulseekStatus: () =>
    request<{ configured: boolean; reachable: boolean; hint?: string }>(
      "/api/acquire/soulseek/status",
    ),
  soulseekSearch: (artist: string, title: string) =>
    request<{ query: string; candidates: SoulseekCandidate[] }>(
      "/api/acquire/soulseek/search",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ artist, title }),
      },
    ),
  soulseekDownload: (candidate: SoulseekCandidate) =>
    request<{ queued: boolean; username: string; filename: string }>(
      "/api/acquire/soulseek/download",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          username: candidate.username,
          filename: candidate.full_path,
          size: candidate.size,
        }),
      },
    ),

  /** Find the best Soulseek match for a track and fetch it. */
  /** The best few matches, ranked, without downloading anything. */
  acquireCandidates: (artist: string, title: string) => {
    const query = new URLSearchParams({ artist, title });
    return request<{
      query: string;
      candidates: SoulseekCandidate[];
      total: number;
    }>(`/api/acquire/candidates?${query.toString()}`);
  },

  acquireTrack: (track: {
    key: string;
    artist: string;
    title: string;
    label?: string;
    year?: string;
    album?: string;
    genre?: string;
    chosen?: SoulseekCandidate;
  }) =>
    request<{ download_id: number; queued: boolean }>("/api/acquire/track", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(track),
    }),
  downloads: (key?: string) =>
    request<Download[]>(
      key ? `/api/acquire/downloads?key=${encodeURIComponent(key)}`
          : "/api/acquire/downloads",
    ),
  download: (id: number) => request<Download>(`/api/acquire/downloads/${id}`),
  downloadFileUrl: (id: number) => `/api/acquire/downloads/${id}/file`,

  watches: () => request<Watch[]>("/api/watches"),
  addWatch: (url: string) =>
    request<Watch>("/api/watches", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url }),
    }),
  deleteWatch: (id: string) =>
    request<{ deleted: boolean }>(`/api/watches/${id}`, { method: "DELETE" }),
  checkWatch: (id: string) =>
    request<{ watch_id: string; checked: number; new: ChannelEntry[] }>(
      `/api/watches/${id}/check`,
      { method: "POST" },
    ),
};

/**
 * Subscribe to a task's progress over Server-Sent Events.
 *
 * Returns a disposer. The backend closes the stream itself once the task
 * reaches a terminal state, so the caller never has to poll for completion.
 */
export function subscribeToTask(
  taskId: string,
  handlers: {
    onUpdate: (state: TaskState) => void;
    onEnd?: () => void;
    onError?: (message: string) => void;
  },
): () => void {
  const source = new EventSource(`/api/tasks/${taskId}/events`);
  let closed = false;

  source.onmessage = (event) => {
    try {
      handlers.onUpdate(JSON.parse(event.data) as TaskState);
    } catch {
      /* a malformed frame is not worth tearing the stream down for */
    }
  };
  source.addEventListener("end", () => {
    closed = true;
    source.close();
    handlers.onEnd?.();
  });
  source.onerror = () => {
    if (closed) return;
    // EventSource reconnects on its own; only surface a persistent failure.
    if (source.readyState === EventSource.CLOSED) {
      handlers.onError?.("Lost connection to the server.");
    }
  };

  return () => {
    closed = true;
    source.close();
  };
}

export function formatTime(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return "0:00";
  const total = Math.floor(seconds);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  return h > 0
    ? `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`
    : `${m}:${String(s).padStart(2, "0")}`;
}

export function formatDuration(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds <= 0) return "—";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} min`;
  const h = Math.floor(minutes / 60);
  const m = minutes % 60;
  return m ? `${h} h ${m}` : `${h} h`;
}

export function formatBytes(bytes: number): string {
  if (!bytes) return "—";
  const units = ["B", "KB", "MB", "GB"];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toFixed(value < 10 && unit > 0 ? 1 : 0)} ${units[unit]}`;
}
