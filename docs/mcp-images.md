# Images returned by MCP tools

MCP tools can return images alongside text or on their own. Turnstone saves supported image
results as attachments and supplies the pixels to the model on its next request. Choose a model
with vision support or configure the existing perception fallback.

The initial contract combines all text into one body, followed by images in their returned image
order. Text and image order are each retained, but placement of captions between images is not
preserved. Tools that need precise associations should number their images explicitly. Normalization
happens at ingestion, before the existing text guard, result limits and attachment persistence.
An image-only result receives a short attachment-count notice as its text body.
Broader interleaved results are tracked separately in
[research issue #1476](https://github.com/turnstonelabs/turnstone/issues/1476).

PNG, JPEG and WebP reuse the upload MIME and byte limits, and images must decode within the
shared pixel and JPEG-scan limits. Transparent PNG/WebP images reuse upload flattening before
storage; already stored images are replayed unchanged.
GIF images are omitted because model APIs differ in their support for that format.
Each result has a combined 4 MiB image budget, measured before and after normalization.
Unsupported, invalid or oversized images produce an omission notice while valid text and other
images remain available. Text across all parts shares the existing tool-result allowance. Images
retain the SDK's explicit success/error status; text beginning with `Error:` cannot change it.
Audio and embedded resources keep their existing text representation; resource links are not
fetched as images.

## Model requests

The Responses and Messages API lanes send native image blocks inside tool results. Chat Completions
lanes place images in a labelled user-role envelope after the complete batch of tool results,
including parallel calls. The envelope identifies the originating calls and marks their images as
tool output. It exists only in the outgoing request and adds no user turn to conversation history.

The built-in MCP web-search adapter returns text and an image-omission notice. Call the MCP tool
directly when its images are needed.

## Viewing images

Image results appear expanded in interactive and coordinator conversations. Offscreen images load
lazily, with space reserved before loading. Collapse the image disclosure to hide it, or follow an
image's link to view the stored image at full size. The same controls are available after reopening
a conversation.

Images use authenticated, workstream-scoped attachment routes, including the owning node's proxy
prefix. A result can appear before its attachment transaction finishes; the viewer retries briefly
and offers a collapse/reopen retry when the image remains unavailable. Reopening retains loaded
images and restarts only failed loads.

## Storage and lifecycle

Attachment bytes stay in the configured database. Conversation turns contain content-hash references;
events and public history contain only attachment IDs and display metadata. Repeated identical
images share one stored blob. Each reference is counted, forks retain their copied references, and
deleting references releases the blob when its count reaches zero. Tool rows and their attachment
references commit atomically, and retrying the same commit does not increment the count twice.
The normalized text-first representation uses the existing storage and fork paths. Reload and fork
retain the same admitted text and ordered references without introducing a new ledger format.

This feature requires no external object store or schema migration. Previously saved binary
placeholders cannot recover the original images; call the tool again to obtain a fresh result.
