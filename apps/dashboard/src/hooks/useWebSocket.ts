import { useEffect, useRef, useState } from 'react';
import { topologyWS } from '@/api/websocket';
import { useClusterStore } from '@/stores/useClusterStore';

export function useTopologyStream() {
  const [connected, setConnected] = useState(false);
  const setInference = useClusterStore(s => s.setInference);
  const mountedRef   = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
    topologyWS.connect();

    const unsub = topologyWS.addListener((data: unknown) => {
      if (!mountedRef.current) return;
      setConnected(topologyWS.isConnected);
      // data is GNNInference shape when coming from /ws/topology/stream
      if (data && typeof data === 'object') {
        setInference(data as Parameters<typeof setInference>[0]);
      }
    });

    // Poll connection status
    const interval = setInterval(() => {
      if (mountedRef.current) setConnected(topologyWS.isConnected);
    }, 2000);

    return () => {
      mountedRef.current = false;
      unsub();
      clearInterval(interval);
    };
  }, [setInference]);

  return { connected };
}

export default useTopologyStream;
