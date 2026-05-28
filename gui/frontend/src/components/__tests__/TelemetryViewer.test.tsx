import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import TelemetryViewer from '../TelemetryViewer';

describe('TelemetryViewer', () => {
  let mockWebSocket: any;

  beforeEach(() => {
    mockWebSocket = {
      close: vi.fn(),
    };
    global.WebSocket = vi.fn(() => mockWebSocket) as any;
    
    // Mock scrollIntoView
    window.HTMLElement.prototype.scrollIntoView = vi.fn();
  });

  it('renders the header correctly', () => {
    render(<TelemetryViewer />);
    expect(screen.getByText('Live Telemetry Sink')).toBeDefined();
  });

  it('connects to the websocket on mount', () => {
    render(<TelemetryViewer />);
    expect(global.WebSocket).toHaveBeenCalledWith('ws://localhost:8000/ws/telemetry');
  });

});
