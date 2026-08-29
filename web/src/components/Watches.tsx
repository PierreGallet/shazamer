import { For, Show, createResource, createSignal } from "solid-js";
import type { ChannelEntry } from "../lib/api";
import { api, formatDuration } from "../lib/api";

/**
 * Follow a channel or an artist.
 *
 * This is the shift from "run an analysis" to "keep digging": point at a
 * YouTube channel, a SoundCloud artist or a Mixcloud series, and checking it
 * lists what has appeared since last time — each one analysable in a click.
 */

export default function Watches() {
  const [watches, { refetch }] = createResource(() => api.watches());
  const [url, setUrl] = createSignal("");
  const [adding, setAdding] = createSignal(false);
  const [error, setError] = createSignal("");
  const [checking, setChecking] = createSignal<string | null>(null);
  const [fresh, setFresh] = createSignal<Record<string, ChannelEntry[]>>({});
  const [queued, setQueued] = createSignal<Record<string, boolean>>({});

  async function add(event: Event) {
    event.preventDefault();
    const value = url().trim();
    if (!value) return;
    setAdding(true);
    setError("");
    try {
      await api.addWatch(value);
      setUrl("");
      await refetch();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not follow that URL");
    } finally {
      setAdding(false);
    }
  }

  async function check(id: string) {
    setChecking(id);
    setError("");
    try {
      const result = await api.checkWatch(id);
      setFresh({ ...fresh(), [id]: result.new });
      await refetch();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not check for new uploads");
    } finally {
      setChecking(null);
    }
  }

  async function analyse(entry: ChannelEntry) {
    setQueued({ ...queued(), [entry.id]: true });
    try {
      await api.analyzeUrl(entry.url);
    } catch (err) {
      setQueued({ ...queued(), [entry.id]: false });
      setError(err instanceof Error ? err.message : "Could not start the analysis");
    }
  }

  async function remove(id: string) {
    await api.deleteWatch(id);
    await refetch();
  }

  return (
    <div class="wrap">
      <form class="url-form" onSubmit={add}>
        <input
          class="input"
          type="url"
          placeholder="A YouTube channel, a SoundCloud artist, a Mixcloud series…"
          value={url()}
          onInput={(e) => setUrl(e.currentTarget.value)}
          disabled={adding()}
        />
        <button class="btn btn-primary" type="submit" disabled={adding()}>
          <Show when={adding()}><span class="spinner" /></Show>
          Follow
        </button>
      </form>

      <Show when={error()}>
        <div class="error-box" style={{ "margin-top": "0.75rem" }}>{error()}</div>
      </Show>

      <Show
        when={watches() && watches()!.length > 0}
        fallback={
          <Show when={!watches.loading}>
            <div class="empty" style={{ "margin-top": "1.5rem" }}>
              <div class="empty-title">Not following anyone yet</div>
              <div class="small">
                Paste a YouTube channel, a SoundCloud artist or a Mixcloud
                series. New uploads are checked four times a day and analysed
                on their own, so a set you would have missed is waiting in
                your library instead of needing to be hunted for.
              </div>
            </div>
          </Show>
        }
      >
        <div class="stack" style={{ "margin-top": "1.5rem" }}>
          <For each={watches()!}>
            {(watch) => (
              <div class="panel stack-sm">
                <div class="row">
                  <div style={{ "min-width": 0, flex: 1 }}>
                    <div class="set-card-title">{watch.title}</div>
                    <div class="tiny faint" style={{ "word-break": "break-all" }}>
                      {watch.url}
                    </div>
                  </div>
                  <span class="tiny faint">
                    {watch.last_checked
                      ? `checked ${watch.last_checked.slice(0, 10)}`
                      : "never checked"}
                  </span>
                  <button
                    class="btn btn-ghost btn-sm"
                    onClick={() => check(watch.id)}
                    disabled={checking() === watch.id}
                  >
                    <Show when={checking() === watch.id}><span class="spinner" /></Show>
                    Check
                  </button>
                  <button
                    class="btn-icon"
                    title="Stop following"
                    onClick={() => remove(watch.id)}
                  >
                    <svg viewBox="0 0 24 24" width="15" height="15" fill="none"
                         stroke="currentColor" stroke-width="2" stroke-linecap="round">
                      <path d="M18 6L6 18M6 6l12 12" />
                    </svg>
                  </button>
                </div>

                <Show when={fresh()[watch.id]}>
                  <Show
                    when={fresh()[watch.id]!.length > 0}
                    fallback={<div class="tiny faint">Nothing new since last check.</div>}
                  >
                    <div class="stack-sm">
                      <span class="eyebrow">
                        {fresh()[watch.id]!.length} new
                      </span>
                      <For each={fresh()[watch.id]!.slice(0, 10)}>
                        {(entry) => (
                          <div class="watch-entry">
                            <span class="watch-entry-title">{entry.title}</span>
                            <Show when={entry.duration}>
                              <span class="tiny faint mono">
                                {formatDuration(entry.duration!)}
                              </span>
                            </Show>
                            <button
                              class="btn btn-ghost btn-sm"
                              disabled={queued()[entry.id]}
                              onClick={() => analyse(entry)}
                            >
                              {queued()[entry.id] ? "Queued" : "Analyse"}
                            </button>
                          </div>
                        )}
                      </For>
                      <Show when={Object.values(queued()).some(Boolean)}>
                        <div class="tiny faint">
                          Queued analyses run in the background — they appear in
                          the Library when they finish.
                        </div>
                      </Show>
                    </div>
                  </Show>
                </Show>
              </div>
            )}
          </For>
        </div>
      </Show>
    </div>
  );
}
