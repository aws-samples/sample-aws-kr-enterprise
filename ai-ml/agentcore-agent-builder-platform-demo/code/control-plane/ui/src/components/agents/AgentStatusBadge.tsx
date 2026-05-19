const statusConfig: Record<string, { label: string; className: string }> = {
  READY: {
    label: 'Running',
    className: 'bg-green-500/20 text-green-400 border-green-500/30',
  },
  CREATING: {
    label: 'Deploying',
    className: 'bg-yellow-500/20 text-yellow-400 border-yellow-500/30',
  },
  UPDATING: {
    label: 'Updating',
    className: 'bg-yellow-500/20 text-yellow-400 border-yellow-500/30',
  },
  CREATE_FAILED: {
    label: 'Unhealthy',
    className: 'bg-red-500/20 text-red-400 border-red-500/30',
  },
  UPDATE_FAILED: {
    label: 'Unhealthy',
    className: 'bg-red-500/20 text-red-400 border-red-500/30',
  },
  DELETING: {
    label: 'Deleting',
    className: 'bg-gray-500/20 text-gray-400 border-gray-500/30',
  },
  NOT_DEPLOYED: {
    label: 'Not Deployed',
    className: 'bg-gray-500/20 text-gray-400 border-gray-500/30',
  },
};

export default function AgentStatusBadge({ status }: { status: string }) {
  const config = statusConfig[status] || statusConfig.NOT_DEPLOYED;
  return (
    <span
      className={`px-2 py-0.5 rounded text-xs font-medium border ${config.className}`}
    >
      {config.label}
    </span>
  );
}
