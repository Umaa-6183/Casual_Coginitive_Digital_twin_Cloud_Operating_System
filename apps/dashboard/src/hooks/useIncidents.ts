import { useEffect, useState } from 'react';
import type { Incident } from '@/types';
import { fetchIncidents } from '@/api/client';
import { useIncidentStore } from '@/stores/useIncidentStore';

export function useIncidents(statusFilter?: string) {
  const { incidents: storedIncidents, setIncidents } = useIncidentStore();
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let mounted = true;

    async function loadIncidents() {
      try {
        setLoading(true);
        setError(null);

        // Fetch incidents without filter first to get all
        const data = await fetchIncidents();

        if (mounted && data.incidents.length > 0) {
          // Update the global store with all incidents
          setIncidents(data.incidents);
        }
      } catch (err) {
        if (mounted) {
          console.warn('Backend incidents unavailable, using stored data:', err);
          setError(null); // Don't show error if we have stored data
        }
      } finally {
        if (mounted) {
          setLoading(false);
        }
      }
    }

    // Load incidents immediately and poll every 5s
    loadIncidents();
    const interval = setInterval(loadIncidents, 5000);

    return () => {
      mounted = false;
      clearInterval(interval);
    };
  }, []); // Remove statusFilter from dependencies

  // Filter incidents locally based on statusFilter
  const filteredIncidents = statusFilter && statusFilter !== 'all'
    ? storedIncidents.filter(inc => inc.status === statusFilter)
    : storedIncidents;

  return { incidents: filteredIncidents, loading, error };
}
