import { Show, createEffect, createResource, createSignal } from "solid-js";
import { useNavigate, useParams } from "@solidjs/router";
import { api, ApiError, formatDuration } from "../lib/api";
import { SignIn } from "./SignIn";

/**
 * Landing on a shared tracklist.
 *
 * Three states, and the order matters. What is on offer is shown *before*
 * anyone is asked to sign in, because "make an account to see what this is"
 * is a worse deal than "make an account to keep this, which is a 74-minute
 * Boiler Room set with 32 tracks in it". Signed in already, it is claimed and
 * you land on it — the sign-in step exists to identify a library to put it
 * in, not as a toll.
 */
export default function SharedSet() {
  const params = useParams();
  const navigate = useNavigate();

  const token = () => params.token ?? "";
  const [share] = createResource(token, (t) => api.peekShare(t));
  const [auth, { refetch: recheck }] = createResource(() => api.me());
  const [claiming, setClaiming] = createSignal(false);
  const [error, setError] = createSignal("");

  async function claim() {
    setClaiming(true);
    setError("");
    try {
      const { set_id } = await api.claimShare(token());
      navigate(`/sets/${set_id}`, { replace: true });
    } catch (err) {
      setError(err instanceof ApiError
        ? err.message
        : "That invitation could not be opened.");
      setClaiming(false);
    }
  }

  /**
   * Signed in and the invitation is good: take it and go.
   *
   * Making someone click "yes, I do want the thing I just clicked a link for"
   * is a step for nobody. An effect rather than a poll — it fires when both
   * answers have arrived, whichever order they land in, and once.
   */
  let taken = false;
  createEffect(() => {
    if (taken || !auth()?.authenticated || !share()) return;
    taken = true;
    void claim();
  });

  return (
    <Show
      when={share.state !== "pending"}
      fallback={<div class="boot" />}
    >
      <Show
        when={share()}
        fallback={
          <div class="signin-wrap">
            <div class="signin">
              <h1 class="signin-title">This link has expired</h1>
              <p class="signin-sub">
                The invitation no longer points anywhere. Ask whoever sent it
                for a fresh one.
              </p>
              <a class="btn btn-ghost signin-btn" href="/">Go to Shazamer</a>
            </div>
          </div>
        }
      >
        {(offer) => (
          <Show
            when={!auth()?.authenticated}
            fallback={
              <div class="signin-wrap">
                <div class="signin">
                  <h1 class="signin-title">{offer().title}</h1>
                  <p class="signin-sub">
                    {claiming() ? "Putting it in your library…" : "One moment…"}
                  </p>
                  <Show when={error()}>
                    <p class="signin-error">{error()}</p>
                  </Show>
                </div>
              </div>
            }
          >
            <div class="signin-wrap">
              <div class="signin">
                <div class="shared-offer">
                  <div class="eyebrow">
                    {offer().from_name} shared a tracklist
                  </div>
                  <h1 class="signin-title">{offer().title}</h1>
                  <p class="signin-sub">
                    {offer().track_count} tracks
                    <Show when={offer().duration}>
                      {" · "}{formatDuration(offer().duration)}
                    </Show>
                  </p>
                  <p class="tiny faint">
                    {/* Said plainly, because it is the part people would
                        otherwise have to guess: it becomes theirs, and
                        nothing the sender does later takes it away. */}
                    Sign in and it becomes your own copy — yours to keep, star
                    and delete, whatever they do with theirs.
                  </p>
                </div>

                <SignIn onSignedIn={recheck} bare />
              </div>
            </div>
          </Show>
        )}
      </Show>
    </Show>
  );
}
