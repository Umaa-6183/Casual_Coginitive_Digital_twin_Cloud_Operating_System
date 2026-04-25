import { create } from 'zustand';
import type { AppSettings } from '@/types';

interface SettingsState {
  settings: AppSettings;
  update:   (patch: Partial<AppSettings>) => void;
}

export const useSettingsStore = create<SettingsState>((set) => ({
  settings: {
    autonomyMode:           'human-in-loop',
    ebpfSampleRate:         100,
    gnnConfidenceThreshold: 85,
    alertCooldown:          30,
    ghostPreviewRequired:   true,
    opaEnforce:             true,
    llmStreaming:           true,
    notifsCritical:         true,
    notifsWarning:          true,
    notifsInfo:             false,
    mttrTarget:             15,
    logRetentionDays:       30,
    replicaLimit:           10,
  },
  update: (patch) =>
    set(s => ({ settings: { ...s.settings, ...patch } })),
}));
