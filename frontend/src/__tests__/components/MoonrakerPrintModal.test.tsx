/**
 * Moonraker direct-print modal (Snapmaker U1 card Print button).
 *
 * The regression that matters: select a file, cancel, reopen — Print must
 * be disabled again. The modal unmounts its file input on close, so a
 * retained selection would let an invisible file be started.
 */

import { describe, it, expect, beforeEach } from 'vitest';
import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { render } from '../utils';
import { PrintersPage } from '../../pages/PrintersPage';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';

const U1_PRINTER = {
  id: 7,
  name: 'Snapmaker U1',
  serial_number: '8110026012700168D0YK',
  ip_address: '192.168.4.102',
  is_active: true,
  model: 'U1',
  nozzle_count: 1,
  auto_archive: true,
  location: null,
  created_at: '2024-01-01T00:00:00Z',
  updated_at: '2024-01-01T00:00:00Z',
};

describe('Moonraker direct print modal', () => {
  beforeEach(() => {
    server.use(
      http.get('/api/v1/printers/', () => HttpResponse.json([U1_PRINTER])),
      http.get('/api/v1/queue/', () => HttpResponse.json([])),
      http.get('/api/v1/printers/:id/status', ({ params }) =>
        HttpResponse.json({
          id: Number(params.id),
          name: 'Snapmaker U1',
          connected: true,
          state: 'IDLE',
          progress: 0,
          layer_num: 0,
          total_layers: 0,
          temperatures: { nozzle: 27, nozzle_target: 0, bed: 25, bed_target: 0 },
          remaining_time: 0,
          ams: [],
          vt_tray: [],
          hms_errors: [],
          nozzles: [],
        }),
      ),
    );
  });

  it('clears the selected file across cancel + reopen', async () => {
    const user = userEvent.setup();
    render(<PrintersPage />);

    const enabledPrintButtons = () =>
      screen.getAllByRole('button', { name: /^print$/i }).filter((b) => !(b as HTMLButtonElement).disabled);

    // Card Print opens the direct-print modal (not the library/queue flow).
    await user.click(await screen.findByRole('button', { name: /^print$/i }));
    expect(await screen.findByText(/Upload a sliced \.gcode file/i)).toBeInTheDocument();
    // Only the card button is enabled; the modal's Print awaits a file.
    expect(enabledPrintButtons()).toHaveLength(1);

    const input = document.querySelector('input[type="file"][accept*=".gcode"]') as HTMLInputElement;
    expect(input).not.toBeNull();
    await user.upload(input, new File(['G28\n'], 'cube.gcode', { type: 'text/plain' }));
    expect(enabledPrintButtons()).toHaveLength(2);

    await user.click(screen.getByRole('button', { name: /cancel/i }));
    expect(screen.queryByText(/Upload a sliced \.gcode file/i)).not.toBeInTheDocument();

    // Reopen: the previously selected file must not ride along invisibly.
    await user.click(screen.getByRole('button', { name: /^print$/i }));
    expect(await screen.findByText(/Upload a sliced \.gcode file/i)).toBeInTheDocument();
    expect(enabledPrintButtons()).toHaveLength(1);
  });
});
