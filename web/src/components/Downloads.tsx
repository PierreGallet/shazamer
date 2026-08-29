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

export default function Downloads() {
  const [rows, { refetch }] = createResource(() => api.downloads());
  const [filter, setFilter] = createSignal("");

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
    const all = rows() ?? [];
    if (!needle) return all;
    return all.filter((d) =>
      `${d.artist} ${d.title} ${d.username}`.toLowerCase().includes(needle));
  };

  return (
    <div class="wrap">
      <div class="row downloads-head">
        <span class="eyebrow">Downloads</span>
        <div class="spacer" />
        <input
          class="signin-input downloads-filter"
          placeholder="Filter"
          value={filter()}
          onInput={(e) => setFilter(e.currentTarget.value)}
        />
      </div>

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
