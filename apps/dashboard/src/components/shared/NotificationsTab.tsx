import React, { useState } from 'react';
import { useIncidentStore } from '@/stores/useIncidentStore';
import type { Notification } from '@/types';

type FilterType = 'all' | Notification['type'];

const TYPE_COLORS: Record<Notification['type'], string> = {
  alert:    '#FF3B5C',
  action:   '#9B5DE5',
  resolved: '#00FF9F',
  policy:   '#00D4FF',
  info:     '#FFB800',
};

const TYPE_ICONS: Record<Notification['type'], string> = {
  alert:    '🚨',
  action:   '⚡',
  resolved: '✅',
  policy:   '🛡',
  info:     'ℹ',
};

const FILTERS: { id: FilterType; label: string }[] = [
  { id: 'all',      label: 'All'      },
  { id: 'alert',    label: 'Alerts'   },
  { id: 'action',   label: 'Actions'  },
  { id: 'resolved', label: 'Resolved' },
  { id: 'policy',   label: 'Policy'   },
  { id: 'info',     label: 'Info'     },
];

export const NotificationsTab: React.FC = () => {
  const { notifications, unreadCount, markAllRead, markRead } = useIncidentStore();
  const [filter, setFilter] = useState<FilterType>('all');

  const filtered = notifications.filter(n => filter === 'all' || n.type === filter);

  return (
    <div style={{ height: '100%', display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
      {/* Header */}
      <div style={{
        padding: '12px 20px', borderBottom: '1px solid #0D2244',
        display: 'flex', justifyContent: 'space-between', alignItems: 'center',
        flexShrink: 0,
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <span style={{ fontSize: 14, fontWeight: 600, color: '#C8D8E8' }}>Notifications</span>
          {unreadCount > 0 && (
            <span style={{
              background: '#FF3B5C', color: '#fff', fontSize: 10, fontWeight: 700,
              borderRadius: 10, padding: '2px 7px',
            }}>
              {unreadCount} unread
            </span>
          )}
        </div>
        <button
          onClick={markAllRead}
          style={{
            background: 'transparent', border: '1px solid #0D2244', borderRadius: 6,
            color: '#4A6A8A', fontSize: 12, padding: '4px 10px', cursor: 'pointer',
          }}
        >
          Mark all read
        </button>
      </div>

      {/* Filter pills */}
      <div style={{
        padding: '10px 20px', borderBottom: '1px solid #0D2244',
        display: 'flex', gap: 8, flexShrink: 0,
      }}>
        {FILTERS.map(f => (
          <button
            key={f.id}
            onClick={() => setFilter(f.id)}
            style={{
              border: filter === f.id ? '1px solid #00D4FF66' : '1px solid #0D2244',
              background: filter === f.id ? '#00D4FF15' : 'transparent',
              color: filter === f.id ? '#00D4FF' : '#4A6A8A',
              borderRadius: 6, padding: '3px 10px', cursor: 'pointer', fontSize: 11,
              transition: 'all 0.15s',
            }}
          >
            {f.label}
          </button>
        ))}
      </div>

      {/* List */}
      <div style={{ flex: 1, overflowY: 'auto', padding: '12px 20px' }}>
        {filtered.length === 0 && (
          <div style={{ textAlign: 'center', color: '#4A6A8A', marginTop: 40, fontSize: 13 }}>
            No notifications
          </div>
        )}
        {filtered.map(n => {
          const color = TYPE_COLORS[n.type];
          return (
            <div
              key={n.id}
              onClick={() => markRead(n.id)}
              style={{
                display: 'flex', alignItems: 'flex-start', gap: 12,
                padding: '12px 14px', marginBottom: 8, borderRadius: 8,
                background: n.read ? '#06111F' : '#06111F',
                border: n.read ? '1px solid #0D2244' : `1px solid ${color}33`,
                cursor: 'pointer', transition: 'border 0.2s',
              }}
            >
              {/* Unread dot */}
              {!n.read && (
                <div style={{
                  width: 6, height: 6, borderRadius: '50%', background: color,
                  boxShadow: `0 0 6px ${color}`, marginTop: 5, flexShrink: 0,
                }} />
              )}
              {n.read && <div style={{ width: 6, flexShrink: 0 }} />}

              <div style={{ fontSize: 16, flexShrink: 0, lineHeight: 1.2 }}>
                {TYPE_ICONS[n.type]}
              </div>

              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ fontSize: 12, color: n.read ? '#8899AA' : '#C8D8E8', lineHeight: 1.5 }}>
                  {n.msg}
                </div>
                <div style={{ display: 'flex', gap: 10, marginTop: 4 }}>
                  <span style={{ fontSize: 10, color: color, fontFamily: 'JetBrains Mono, monospace' }}>
                    {n.source}
                  </span>
                  <span style={{ fontSize: 10, color: '#4A6A8A', fontFamily: 'JetBrains Mono, monospace' }}>
                    {n.time}
                  </span>
                </div>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
};

export default NotificationsTab;
