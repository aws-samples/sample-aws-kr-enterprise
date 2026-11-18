// Agent answers and reports are Markdown written by a model: images are shown as text so nothing is fetched from
// addresses in that text (the Web UI's Content-Security-Policy blocks them as well).
export const markdownComponents = {
  img: ({ alt, src }) => <span>[이미지: {alt || src}]</span>,
};
