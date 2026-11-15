# Recover Douyin content

Resolve short links to a canonical `/video/POST_ID` or `/note/POST_ID` page. Preserve
both URLs and verify that metadata and media belong to that exact ID.

Prefer a matching first-party `aweme/detail` response. A detail response for a different
item can come from a recommendation feed; discard it. For image posts, use the ordered
`images` list to establish the expected count and retain all URL candidates per slide.

If the ordinary page omits the content, use the configured browser to render the
canonical page and inspect embedded state and first-party responses. A crawler user agent
may expose an SEO-rendered title, caption, or body, but this is a fallback to verify,
not a guaranteed route. HTTP 200 and a recommendation feed do not prove successful capture.

Keep page text and media evidence distinct. A DOM image list can omit unloaded slides
or include recommendations; record its total and post association as unverified until
matching metadata or direct inspection establishes them. Avoid claiming a complete
carousel from a count of discovered images alone.

If a prompt appears only in public comments, preserve that surface and commenter
attribution. Repetition can corroborate wording but cannot establish creator authorship.
Cross-check important wording against the creator's visible prompt when accessible;
label recovered, translated, or adapted wording separately.

Stop for user interaction at a login wall or captcha. If canonical rendering and
first-party metadata omit the needed content, request the media or relevant screenshots.
Keep partial extracted material available and describe its gaps. Avoid substituting an
uncited search snippet or a generic downloader relay for the requested post.
