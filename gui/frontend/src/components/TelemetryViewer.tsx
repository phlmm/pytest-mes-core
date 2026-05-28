import { useEffect, useState, useRef } from 'react';

interface LogLine {
  text: string;
  id: number;
}

export default function TelemetryViewer() {
  const [logs, setLogs] = useState<LogLine[]>([]);
  const ws = useRef<WebSocket | null>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  const logId = useRef(0);

  useEffect(() => {
    // Connect to FastAPI websocket
    const socket = new WebSocket('ws://localhost:8000/ws/telemetry');
    ws.current = socket;

    socket.onmessage = (event) => {
      const newLine = event.data;
      logId.current += 1;
      setLogs((prevLogs) => {
        const updated = [...prevLogs, { text: newLine, id: logId.current }];
        // Keep last 1000 lines to prevent memory explosion
        return updated.slice(-1000);
      });
    };

    return () => {
      socket.close();
    };
  }, []);

  useEffect(() => {
    // Auto-scroll to bottom
    if (bottomRef.current) {
      bottomRef.current.scrollIntoView({ behavior: 'smooth' });
    }
  }, [logs]);

  // Basic color coding for factory logs
  const getColorClass = (text: string) => {
    if (text.includes('FAIL') || text.includes('ERROR') || text.includes('FAULT')) return 'text-red-400';
    if (text.includes('WARN')) return 'text-yellow-400';
    if (text.includes('PASS') || text.includes('SUCCESS')) return 'text-green-400';
    return 'text-slate-300';
  };

  return (
    <div className="flex flex-col h-full bg-slate-950 rounded-md border border-slate-800 p-4 font-mono text-sm overflow-hidden shadow-inner">
      <h2 className="text-slate-400 font-bold mb-4 uppercase tracking-widest text-xs">Live Telemetry Sink</h2>
      <div className="flex-1 overflow-y-auto space-y-1 pr-2 custom-scrollbar">
        {logs.map((log) => (
          <div key={log.id} className={`${getColorClass(log.text)} break-all`}>
            {log.text}
          </div>
        ))}
        <div ref={bottomRef} />
      </div>
    </div>
  );
}
