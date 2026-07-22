export default function Header({ title }: { title: string }) {
  return (
    <header className="h-14 border-b border-[var(--border)] flex items-center px-6">
      <h1 className="text-lg font-semibold text-white">{title}</h1>
    </header>
  );
}
