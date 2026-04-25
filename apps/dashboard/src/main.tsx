import React from 'react';
import ReactDOM from 'react-dom/client';
import { Layout } from '@/components/shared/Layout';

const root = document.getElementById('root');
if (!root) throw new Error('Root element not found');

ReactDOM.createRoot(root).render(
  <React.StrictMode>
    <Layout />
  </React.StrictMode>,
);
