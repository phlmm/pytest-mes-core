import { useState, useEffect } from 'react';
import TelemetryViewer from './components/TelemetryViewer';

function App() {
  const [operatorId, setOperatorId] = useState('OPERATOR-01');
  const [isRunning, setIsRunning] = useState(false);
  const [statusMessage, setStatusMessage] = useState('Idle');

  // Check initial status
  useEffect(() => {
    fetch('http://localhost:8000/api/status')
      .then(res => res.json())
      .then(data => {
        setIsRunning(data.is_running);
        if (data.is_running) setStatusMessage('Testing in progress...');
      })
      .catch(err => console.error('Failed to fetch status:', err));
  }, []);

  const handleStart = async () => {
    try {
      setStatusMessage('Starting test...');
      const res = await fetch('http://localhost:8000/api/run', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ operator_id: operatorId, project_path: 'tests/' })
      });
      if (res.ok) {
        setIsRunning(true);
        setStatusMessage('Testing in progress...');
      } else {
        const error = await res.json();
        setStatusMessage(`Error: ${error.detail}`);
      }
    } catch (err) {
      setStatusMessage(`Network Error`);
    }
  };

  const handleStop = async () => {
    try {
      setStatusMessage('Stopping test...');
      await fetch('http://localhost:8000/api/stop', { method: 'POST' });
      setIsRunning(false);
      setStatusMessage('Test Aborted. Idle.');
    } catch (err) {
      setStatusMessage(`Network Error`);
    }
  };

  return (
    <div className="h-screen w-full flex bg-slate-900 text-slate-50 p-6 gap-6 font-sans">
      
      {/* Left Pane: Controls */}
      <div className="w-1/3 flex flex-col gap-6">
        <div className="bg-slate-800 rounded-xl p-6 shadow-lg border border-slate-700">
          <h1 className="text-2xl font-bold mb-1 text-white tracking-tight">pytest-mes-core</h1>
          <p className="text-slate-400 text-sm mb-6">Hardware Validation UI</p>

          <div className="mb-6">
            <label className="block text-xs font-semibold text-slate-400 uppercase tracking-wider mb-2">Operator ID</label>
            <input 
              type="text" 
              value={operatorId}
              onChange={(e) => setOperatorId(e.target.value)}
              className="w-full bg-slate-900 border border-slate-600 rounded-md py-3 px-4 text-lg text-white focus:outline-none focus:ring-2 focus:ring-blue-500 transition-all"
              placeholder="Scan badge..."
              disabled={isRunning}
            />
          </div>

          <div className="flex gap-4 mt-8">
            <button 
              onClick={handleStart}
              disabled={isRunning}
              className={`flex-1 py-4 rounded-md font-bold text-lg transition-all ${
                isRunning 
                  ? 'bg-slate-700 text-slate-500 cursor-not-allowed' 
                  : 'bg-blue-600 hover:bg-blue-500 text-white shadow-[0_0_15px_rgba(37,99,235,0.4)]'
              }`}
            >
              START TEST
            </button>
            <button 
              onClick={handleStop}
              disabled={!isRunning}
              className={`flex-1 py-4 rounded-md font-bold text-lg transition-all ${
                !isRunning 
                  ? 'bg-slate-700 text-slate-500 cursor-not-allowed' 
                  : 'bg-red-600 hover:bg-red-500 text-white shadow-[0_0_15px_rgba(220,38,38,0.4)]'
              }`}
            >
              E-STOP
            </button>
          </div>
        </div>

        <div className="bg-slate-800 rounded-xl p-6 shadow-lg border border-slate-700 flex-1">
          <h2 className="text-xs font-semibold text-slate-400 uppercase tracking-wider mb-2">System Status</h2>
          <div className="flex items-center gap-3">
            <div className={`w-4 h-4 rounded-full ${isRunning ? 'bg-blue-500 animate-pulse' : 'bg-slate-500'}`}></div>
            <span className="text-lg font-medium">{statusMessage}</span>
          </div>
        </div>
      </div>

      {/* Right Pane: Telemetry */}
      <div className="w-2/3">
        <TelemetryViewer />
      </div>

    </div>
  );
}

export default App;
