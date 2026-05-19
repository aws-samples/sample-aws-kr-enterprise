'use client';

import Link from 'next/link';
import { usePathname } from 'next/navigation';
import type { LucideIcon } from 'lucide-react';
import {
  LayoutDashboard, Bot, Wrench, LogOut, BookOpen, Presentation,
  PanelLeftClose, PanelLeftOpen, Activity,
} from 'lucide-react';

const navItems = [
  { href: '/', label: 'Dashboard', icon: LayoutDashboard },
  { href: '/agents', label: 'Agent Registry', icon: Bot },
  { href: '/builder', label: 'Agent Builder', icon: Wrench },
  { href: '/traces', label: 'Trace Viewer', icon: Activity },
];

const docItems = [
  { href: '/architecture', label: 'Architecture', icon: BookOpen },
  { href: '/presentation', label: 'Presentation', icon: Presentation },
];

interface SidebarProps {
  collapsed: boolean;
  onToggle: () => void;
}

export default function Sidebar({ collapsed, onToggle }: SidebarProps) {
  const pathname = usePathname();

  const renderNavItem = (item: { href: string; label: string; icon: LucideIcon }) => {
    const Icon = item.icon;
    const isActive = pathname === item.href;
    return (
      <Link
        key={item.href}
        href={item.href}
        title={collapsed ? item.label : undefined}
        className={`flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm transition-colors ${
          isActive
            ? 'bg-[var(--lime)]/10 text-[var(--lime)] font-medium'
            : 'text-[var(--text-dim)] hover:bg-[var(--surface-hover)] hover:text-white'
        } ${collapsed ? 'justify-center' : ''}`}
      >
        <Icon size={18} />
        {!collapsed && <span>{item.label}</span>}
      </Link>
    );
  };

  return (
    <aside className={`${collapsed ? 'w-20' : 'w-64'} bg-[var(--surface)] border-r border-[var(--border)] h-screen flex flex-col transition-all duration-200`}>
      <div className={`flex items-center shrink-0 ${collapsed ? 'justify-center' : 'justify-between'} px-4 py-4`}>
        {!collapsed && (
          <div className="text-xl font-bold text-white">
            <span className="text-[var(--purple)]">AIOps</span> Platform
          </div>
        )}
        <button
          onClick={onToggle}
          className="text-[var(--text-dim)] hover:text-white transition-colors p-1"
          title={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
        >
          {collapsed ? <PanelLeftOpen size={20} /> : <PanelLeftClose size={20} />}
        </button>
      </div>

      <nav className="flex-1 overflow-y-auto px-3 space-y-1">
        {navItems.map(renderNavItem)}
        <div className="border-t border-[var(--border)] my-3" />
        {docItems.map(renderNavItem)}
      </nav>

      <div className="shrink-0 px-3 py-3 border-t border-[var(--border)]">
        <button
          onClick={() => { window.location.href = '/api/auth/logout'; }}
          className={`flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm text-[var(--text-dim)] hover:bg-[var(--surface-hover)] hover:text-white transition-colors w-full ${collapsed ? 'justify-center' : ''}`}
          title={collapsed ? 'Logout' : undefined}
        >
          <LogOut size={18} />
          {!collapsed && <span>Logout</span>}
        </button>
      </div>
    </aside>
  );
}
