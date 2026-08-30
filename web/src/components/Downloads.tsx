import { For, Show, createResource, createSignal, onCleanup } from "solid-js";
import { api, formatBytes } from "../lib/api";

/**
 * Everything fetched from Soulseek, and a way to save it again.
 *
 * Until now a download could only be saved in the moment it finished — from
 * the panel that started it, on the page it was started from. Close the tab
 * and the file was on the server with no way to reach it, which is the same
 * as not having fetched it.
 *
 * Sorted newest first, because the reason to open this is usually the thing
 * you just did.
 */

const POLL_MS = 4000;

type SortBy = "recent" | "bpm" | "key";


export default function Downloads() {
  const [rows, { refetch }] = createResource(() => api.downloads());
  const [filter, setFilter] = createSignal("");
  const [sort, setSort] = createSignal<SortBy>("recent");
  const [sweeping, setSweeping] = createSignal(false);
  const [swept, setSwept] = createSignal("");

  // Polled only while something is moving. A queue on Soulseek can sit for
  // ten minutes, and a page that stops updating during it looks broken.
  let timer: number | undefined;
  const tick = () => {
    const busy = (rows() ?? []).some(
      (d) => d.status !== "ready" && d.status !== "failed");
    if (busy) refetch();
    timer = window.setTimeout(tick, POLL_MS);
  };
  timer = window.setTimeout(tick, POLL_MS);
  onCleanup(() => window.clearTimeout(timer));

  const shown = () => {
    const needle = filter().trim().toLowerCase();
    let all = rows() ?? [];
    if (needle) {
      // Style is searchable too: "tech house" is how you find a record whose
      // name you have forgotten, which is the whole point of a crate.
      all = all.filter((d) =>
        `${d.artist} ${d.title} ${d.username} ${d.style ?? ""} ${d.genre ?? ""}`
          .toLowerCase().includes(needle));
    }
    const by = sort();
    if (by === "recent") return all;
    // Rows with nothing measured sink rather than sorting as zero — a file
    // waiting to be described is not a 0 BPM record.
    return [...all].sort((a, b) => {
      if (by === "bpm") return (b.bpm ?? -1) - (a.bpm ?? -1);
      return (a.camelot ?? "~").localeCompare(b.camelot ?? "~", undefined,
                                              { numeric: true });
    });
  };

  const undescribed = () =>
    (rows() ?? []).filter((d) => d.status === "ready" && !d.analysed_at).length;

  async function describe() {
    setSweeping(true);
    setSwept("");
    try {
      const report = await api.describeDownloads();
      // The sweep is measured in files, not in a spinner: a crate of two
      // hundred takes hours and "working..." for hours is indistinguishable
      // from broken.
      setSwept(
        report.unavailable
          ? "This server cannot measure audio — Essentia is not installed here."
          : `${report.described} described` +
            (report.skipped ? `, ${report.skipped} no longer on disk` : "") +
            (report.failed ? `, ${report.failed} unreadable` : ""));
      refetch();
    } catch (err) {
      setSwept(err instanceof Error ? err.message : "Could not start the sweep");
    } finally {
      setSweeping(false);
    }
  }

  return (
    <div class="wrap">
      <div class="row downloads-head">
        <span class="eyebrow">Downloads</span>
        <div class="spacer" />
        <input
          class="signin-input downloads-filter"
          placeholder="Filter by name or style"
          value={filter()}
          onInput={(e) => setFilter(e.currentTarget.value)}
        />
        <select
          class="signin-input downloads-sort"
          aria-label="Sort the crate"
          value={sort()}
          onChange={(e) => setSort(e.currentTarget.value as SortBy)}
        >
          <option value="recent">Newest first</option>
          <option value="bpm">Tempo</option>
          <option value="key">Key</option>
        </select>
        <Show when={undescribed()}>
          <button
            class="btn btn-ghost btn-sm"
            disabled={sweeping()}
            title="Measure tempo, key, loudness and style for files nobody has looked at yet. Minutes per track, so it runs on request rather than on its own."
            onClick={describe}
          >
            {sweeping() ? "Measuring…" : `Measure ${undescribed()}`}
          </button>
        </Show>
      </div>

      <Show when={swept()}>
        <div class="tiny faint downloads-swept">{swept()}</div>
      </Show>

      <Show
        when={shown().length}
        fallback={
          <div class="empty">
            <div class="empty-title">
              {rows()?.length ? "Nothing matches that" : "No downloads yet"}
            </div>
            <div class="tiny faint">
              {rows()?.length
                ? "Try a different name."
                : "Open a set, pick a track, and fetch it from Soulseek."}
            </div>
          </div>
        }
      >
        <div class="downloads">
          <For each={shown()}>
            {(item) => (
              <div class="download-row">
                <div class="download-name">
                  <div class="download-title">
                    {item.artist} — {item.title}
                  </div>
                  <div class="tiny faint">
                    <Show when={item.username}>
                      from {item.username}
                      {" · "}
                    </Show>
                    <Show when={item.quality}>{item.quality}{" · "}</Show>
                    <Show when={item.size}>{formatBytes(item.size)}{" · "}</Show>
                    {item.message}
                  </div>
                </div>

                <Show when={item.bpm}>
                  <span class="chip" title="The record's own tempo, measured from this file">
                    {item.bpm!.toFixed(0)}
                  </span>
                </Show>
                <Show when={item.camelot}>
                  <span class="chip chip-key" title={item.musical_key ?? ""}>
                    {item.camelot}
                  </span>
                </Show>
                <Show when={item.style || item.genre}>
                  {/* Where it came from is part of what it is: a style read
                      from a stranger's ID3 tag and one from a Discogs release
                      are not equally trustworthy. */}
                  <span
                    class="chip chip-style"
                    classList={{ faint: item.style_source === "tag" }}
                    title={item.style_source === "discogs"
                      ? "Style, from Discogs"
                      : "Genre, from the file's own tags — whoever ripped it wrote this"}
                  >
                    {item.style || item.genre}
                  </span>
                </Show>

                <Show when={item.quality_note}>
                  {/* The declared bitrate against what the audio carries. Only
                      shown when they disagree — a note here is a finding, and
                      "the bitrate is what it says" on every row would be
                      noise. */}
                  <span class="chip chip-warn" title={item.quality_note}>
                    quality
                  </span>
                </Show>

                <Show when={item.verified}>
                  {/* Only shown when true: "unverified" on every row would be
                      noise, and this one means the audio was fingerprinted
                      and found to be the record it claims to be. */}
                  <span class="chip chip-key" title="Fingerprinted and matched">
                    verified
                  </span>
                </Show>

                <span
                  class="chip"
                  classList={{
                    "chip-accent": item.status === "ready",
                    "chip-warn": item.status === "failed",
                  }}
                >
                  {item.status}
                </span>

                <Show
                  when={item.available}
                  fallback={
                    <span class="tiny faint download-gone">
                      {item.status === "ready" ? "file swept" : "—"}
                    </span>
                  }
                >
                  <a class="btn btn-ghost btn-sm"
                     href={api.downloadFileUrl(item.id)}
                     download="">
                    Save
                  </a>
                </Show>
              </div>
            )}
          </For>
        </div>
      </Show>
    </div>
  );
}
