export default function LoadingSpinner({
  text = 'Loading...',
}: {
  text?: string;
}) {
  return (
    <div className="flex items-center gap-3 text-[#94a3b8]">
      <div className="w-5 h-5 border-2 border-[#6366f1] border-t-transparent rounded-full animate-spin" />
      <span className="text-sm">{text}</span>
    </div>
  );
}
