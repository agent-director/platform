import React from 'react';

export const metadata = {
  title: 'Agent Director',
  description: 'Unified Dashboard for  Agent Control Plane',
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body style={{ fontFamily: 'system-ui, sans-serif', margin: 0, padding: 0, backgroundColor: '#f9fafb' }}>
        <header style={{ backgroundColor: '#111827', color: 'white', padding: '1rem' }}>
          <h1 style={{ margin: 0, fontSize: '1.25rem' }}>Agent Director</h1>
        </header>
        <main style={{ padding: '2rem' }}>
          {children}
        </main>
      </body>
    </html>
  );
}
