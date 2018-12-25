"""
.. moduleauthor:: Fabio Manganiello <blacklight86@gmail.com>
"""

import json
import math
import os
import queue
import tempfile
import time

from enum import Enum
from threading import Thread, Event, RLock

from .core import Sound
from platypush.plugins import Plugin, action


class PlaybackState(Enum):
    STOPPED='STOPPED',
    PLAYING='PLAYING',
    PAUSED='PAUSED'


class RecordingState(Enum):
    STOPPED='STOPPED',
    RECORDING='RECORDING',
    PAUSED='PAUSED'


class SoundPlugin(Plugin):
    """
    Plugin to interact with a sound device.

    Requires:

        * **sounddevice** (``pip install sounddevice``)
        * **soundfile** (``pip install soundfile``)
        * **numpy** (``pip install numpy``)
    """

    def __init__(self, input_device=None, output_device=None,
                 input_blocksize=Sound._DEFAULT_BLOCKSIZE,
                 output_blocksize=Sound._DEFAULT_BLOCKSIZE,
                 playback_bufsize=Sound._DEFAULT_BUFSIZE, *args, **kwargs):
        """
        :param input_device: Index or name of the default input device. Use :method:`platypush.plugins.sound.query_devices` to get the available devices. Default: system default
        :type input_device: int or str

        :param output_device: Index or name of the default output device. Use :method:`platypush.plugins.sound.query_devices` to get the available devices. Default: system default
        :type output_device: int or str

        :param input_blocksize: Blocksize to be applied to the input device. Try to increase this value if you get input overflow errors while recording. Default: 2048
        :type input_blocksize: int

        :param output_blocksize: Blocksize to be applied to the output device. Try to increase this value if you get output underflow errors while playing. Default: 2048
        :type output_blocksize: int

        :param playback_bufsize: Number of audio blocks that will be cached while playing (default: 20)
        :type playback_bufsize: int
        """

        super().__init__(*args, **kwargs)

        self.input_device = input_device
        self.output_device = output_device
        self.input_blocksize = input_blocksize
        self.output_blocksize = output_blocksize
        self.playback_bufsize = playback_bufsize

        self.playback_state = {}
        self.playback_state_lock = RLock()
        self.playback_paused_changed = {}
        self.recording_state = RecordingState.STOPPED
        self.recording_state_lock = RLock()
        self.recording_paused_changed = Event()
        self.active_streams = {}
        self.completed_callback_events = {}

    def _get_default_device(self, category):
        """
        Query the default audio devices.

        :param category: Device category to query. Can be either input or output
        :type category: str
        """

        import sounddevice as sd
        return sd.query_hostapis()[0].get('default_' + category.lower() + '_device')

    @action
    def query_devices(self, category=None):
        """
        Query the available devices

        :param category: Device category to query. Can be either input or output. Default: None (query all devices)
        :type category: str

        :returns: A dictionary representing the available devices. Example::

            [
                {
                    "name": "pulse",
                    "hostapi": 0,
                    "max_input_channels": 32,
                    "max_output_channels": 32,
                    "default_low_input_latency": 0.008684807256235827,
                    "default_low_output_latency": 0.008684807256235827,
                    "default_high_input_latency": 0.034807256235827665,
                    "default_high_output_latency": 0.034807256235827665,
                    "default_samplerate": 44100
                },
                {
                    "name": "default",
                    "hostapi": 0,
                    "max_input_channels": 32,
                    "max_output_channels": 32,
                    "default_low_input_latency": 0.008684807256235827,
                    "default_low_output_latency": 0.008684807256235827,
                    "default_high_input_latency": 0.034807256235827665,
                    "default_high_output_latency": 0.034807256235827665,
                    "default_samplerate": 44100
                }
            ]

        """

        import sounddevice as sd

        devs = sd.query_devices()
        if category == 'input':
            devs = [d for d in devs if d.get('max_input_channels') > 0]
        elif category == 'output':
            devs = [d for d in devs if d.get('max_output_channels') > 0]

        return devs

    def _play_audio_callback(self, q, blocksize, streamtype, stream_index):
        import sounddevice as sd

        is_raw_stream = streamtype == sd.RawOutputStream

        def audio_callback(outdata, frames, time, status):
            if self._get_playback_state(stream_index) == PlaybackState.STOPPED:
                raise sd.CallbackStop

            while self._get_playback_state(stream_index) == PlaybackState.PAUSED:
                self.playback_paused_changed[stream_index].wait()

            assert frames == blocksize
            if status.output_underflow:
                self.logger.warning('Output underflow: increase blocksize?')
                outdata = (b'\x00' if is_raw_stream else 0.) * len(outdata)
                return

            assert not status

            try:
                data = q.get_nowait()
            except queue.Empty:
                self.logger.warning('Buffer is empty: increase buffersize?')
                raise sd.CallbackAbort

            if len(data) < len(outdata):
                outdata[:len(data)] = data
                outdata[len(data):] = (b'\x00' if is_raw_stream else 0.) * \
                    (len(outdata) - len(data))
            else:
                outdata[:] = data

        return audio_callback


    @action
    def play(self, file=None, sound=None, device=None, blocksize=None,
             bufsize=Sound._DEFAULT_BUFSIZE, samplerate=None, channels=None,
             stream_index=None):
        """
        Plays a sound file (support formats: wav, raw) or a synthetic sound.

        :param file: Sound file path. Specify this if you want to play a file
        :type file: str

        :param sound: Sound to play. Specify this if you want to play
            synthetic sounds. You can also create polyphonic sounds by just
            calling play multple times.
        :type sound: Sound. You can initialize it either from a list
            of `Sound` objects or from its JSON representation, e.g.:

                {
                    "midi_note": 69,  # 440 Hz A
                    "gain":      1.0, # Maximum volume
                    "duration":  1.0  # 1 second or until release/pause/stop
                }

        :param device: Output device (default: default configured device or
            system default audio output if not configured)
        :type device: int or str

        :param blocksize: Audio block size (default: configured
            `output_blocksize` or 2048)
        :type blocksize: int

        :param bufsize: Size of the audio buffer (default: 20)
        :type bufsize: int

        :param samplerate: Audio samplerate. Default: audio file samplerate if
            in file mode, 44100 Hz if in synth mode
        :type samplerate: int

        :param channels: Number of audio channels. Default: number of channels
            in the audio file in file mode, 1 if in synth mode
        :type channels: int

        :param stream_index: If specified, play to an already active stream
            index (you can get them through
            :method:`platypush.plugins.sound.query_streams`). Default:
            creates a new audio stream through PortAudio.
        """

        if not file and not sound:
            raise RuntimeError('Please specify either a file to play or a ' +
                               'list of sound objects')

        import sounddevice as sd

        if blocksize is None:
            blocksize = self.output_blocksize

        q = queue.Queue(maxsize=bufsize)
        f = None
        t = 0.

        if file:
            file = os.path.abspath(os.path.expanduser(file))

        if device is None:
            device = self.output_device
        if device is None:
            device = self._get_default_device('output')

        try:
            if file:
                import soundfile as sf
                f = sf.SoundFile(file)
            if not samplerate:
                samplerate = f.samplerate if f else Sound._DEFAULT_SAMPLERATE
            if not channels:
                channels = f.channels if f else 1

            self.logger.info('Starting playback of {} to sound device [{}]'.
                             format(file or sound, device))

            if sound:
                sound = Sound.build(sound)

            # Audio queue pre-fill loop
            for _ in range(bufsize):
                if f:
                    data = f.buffer_read(blocksize, dtype='float32')
                    if not data:
                        break
                else:
                    blocktime = float(blocksize / samplerate)
                    next_t = min(t+blocktime, sound.duration) \
                        if sound.duration is not None else t+blocktime

                    data = sound.get_wave(t_start=t, t_end=next_t,
                                          samplerate=samplerate)
                    t = next_t

                    if sound.duration is not None and t >= sound.duration:
                        break

                q.put_nowait(data)  # Pre-fill the audio queue


            if stream_index is None:
                streamtype = sd.RawOutputStream if file else sd.OutputStream
                stream_index = self._allocate_stream_index()
                completed_callback_event = self.completed_callback_events[stream_index]
                stream = streamtype(samplerate=samplerate, blocksize=blocksize,
                                    device=device, channels=channels,
                                    dtype='float32',
                                    callback=self._play_audio_callback(
                                        q=q, blocksize=blocksize,
                                        streamtype=streamtype,
                                        stream_index=stream_index),
                                    finished_callback=completed_callback_event.set)

                self._start_playback(stream_index=stream_index, stream=stream)
            else:
                stream = self.active_streams[stream_index]
                completed_callback_event = self.completed_callback_events[stream_index]

            with stream:
                # Timeout set until we expect all the buffered blocks to
                # be consumed
                timeout = blocksize * bufsize / samplerate

                while True:
                    while self._get_playback_state(stream_index) == \
                            PlaybackState.PAUSED:
                        self.playback_paused_changed[stream_index].wait()

                    if f:
                        data = f.buffer_read(blocksize, dtype='float32')
                        if not data:
                            break
                    else:
                        blocktime = float(blocksize / samplerate)
                        next_t = min(t+blocktime, sound.duration) \
                            if sound.duration is not None else t+blocktime

                        data = sound.get_wave(t_start=t, t_end=next_t,
                                              samplerate=samplerate)
                        t = next_t

                        if sound.duration is not None and t >= sound.duration:
                            break

                    if self._get_playback_state(stream_index) == \
                            PlaybackState.STOPPED:
                        break

                    try:
                        q.put(data, timeout=timeout)
                    except queue.Full as e:
                        if self._get_playback_state(stream_index) != \
                                PlaybackState.PAUSED:
                            raise e

                completed_callback_event.wait()
        except queue.Full as e:
            if stream_index is None or \
                    self._get_playback_state(stream_index) != PlaybackState.STOPPED:
                self.logger.warning('Playback timeout: audio callback failed?')
        finally:
            if f and not f.closed:
                f.close()
                f = None

            self.stop_playback([stream_index])


    @action
    def record(self, file=None, duration=None, device=None, sample_rate=None,
               blocksize=None, latency=0, channels=1, subtype='PCM_24'):
        """
        Records audio to a sound file (support formats: wav, raw)

        :param file: Sound file (default: the method will create a temporary file with the recording)
        :type file: str

        :param duration: Recording duration in seconds (default: record until stop event)
        :type duration: float

        :param device: Input device (default: default configured device or system default audio input if not configured)
        :type device: int or str

        :param sample_rate: Recording sample rate (default: device default rate)
        :type sample_rate: int

        :param blocksize: Audio block size (default: configured `input_blocksize` or 2048)
        :type blocksize: int

        :param latency: Device latency in seconds (default: 0)
        :type latency: float

        :param channels: Number of channels (default: 1)
        :type channels: int

        :param subtype: Recording subtype - see `soundfile docs <https://pysoundfile.readthedocs.io/en/0.9.0/#soundfile.available_subtypes>`_ for a list of the available subtypes (default: PCM_24)
        :type subtype: str
        """

        import sounddevice as sd

        self.recording_paused_changed.clear()

        if file:
            file = os.path.abspath(os.path.expanduser(file))
        else:
            file = tempfile.mktemp(prefix='platypush_recording_', suffix='.wav',
                                   dir='')

        if os.path.isfile(file):
            self.logger.info('Removing existing audio file {}'.format(file))
            os.unlink(file)

        if device is None:
            device = self.input_device
        if device is None:
            device = self._get_default_device('input')

        if sample_rate is None:
            dev_info = sd.query_devices(device, 'input')
            sample_rate = int(dev_info['default_samplerate'])

        if blocksize is None:
            blocksize = self.input_blocksize

        q = queue.Queue()

        def audio_callback(indata, frames, time, status):
            while self._get_recording_state() == RecordingState.PAUSED:
                self.recording_paused_changed.wait()

            if status:
                self.logger.warning('Recording callback status: {}'.format(
                    str(status)))

            q.put(indata.copy())


        try:
            import soundfile as sf
            import numpy

            with sf.SoundFile(file, mode='x', samplerate=sample_rate,
                              channels=channels, subtype=subtype) as f:
                with sd.InputStream(samplerate=sample_rate, device=device,
                                    channels=channels, callback=audio_callback,
                                    latency=latency, blocksize=blocksize):
                    self.start_recording()
                    self.logger.info('Started recording from device [{}] to [{}]'.
                                    format(device, file))

                    recording_started_time = time.time()

                    while self._get_recording_state() != RecordingState.STOPPED \
                            and (duration is None or
                                 time.time() - recording_started_time < duration):
                        while self._get_recording_state() == RecordingState.PAUSED:
                            self.recording_paused_changed.wait()

                        get_args = {
                            'block': True,
                            'timeout': max(0, duration - (time.time() -
                                                          recording_started_time))
                        } if duration is not None else {}

                        data = q.get(**get_args)
                        f.write(data)

                f.flush()

        except queue.Empty as e:
            self.logger.warning('Recording timeout: audio callback failed?')
        finally:
            self.stop_recording()


    @action
    def recordplay(self, duration=None, input_device=None, output_device=None,
                   sample_rate=None, blocksize=None, latency=0, channels=1,
                   dtype=None):
        """
        Records audio and plays it on an output sound device (audio pass-through)

        :param duration: Recording duration in seconds (default: record until stop event)
        :type duration: float

        :param input_device: Input device (default: default configured device or system default audio input if not configured)
        :type input_device: int or str

        :param output_device: Output device (default: default configured device or system default audio output if not configured)
        :type output_device: int or str

        :param sample_rate: Recording sample rate (default: device default rate)
        :type sample_rate: int

        :param blocksize: Audio block size (default: configured `output_blocksize` or 2048)
        :type blocksize: int

        :param latency: Device latency in seconds (default: 0)
        :type latency: float

        :param channels: Number of channels (default: 1)
        :type channels: int

        :param dtype: Data type for the recording - see `soundfile docs <https://python-sounddevice.readthedocs.io/en/0.3.12/_modules/sounddevice.html#rec>`_ for available types (default: input device default)
        :type dtype: str
        """

        import sounddevice as sd

        self.recording_paused_changed.clear()

        if input_device is None:
            input_device = self.input_device
        if input_device is None:
            input_device = self._get_default_device('input')

        if output_device is None:
            output_device = self.output_device
        if output_device is None:
            output_device = self._get_default_device('output')

        if sample_rate is None:
            dev_info = sd.query_devices(input_device, 'input')
            sample_rate = int(dev_info['default_samplerate'])

        if blocksize is None:
            blocksize = self.output_blocksize

        def audio_callback(indata, outdata, frames, time, status):
            while self._get_recording_state() == RecordingState.PAUSED:
                self.recording_paused_changed.wait()

            if status:
                self.logger.warning('Recording callback status: {}'.format(
                    str(status)))

            outdata[:] = indata


        stream_index = None

        try:
            import soundfile as sf
            import numpy

            stream_index = self._allocate_stream_index()
            stream = sd.Stream(samplerate=sample_rate, channels=channels,
                               blocksize=blocksize, latency=latency,
                               device=(input_device, output_device),
                               dtype=dtype, callback=audio_callback)
            self.start_recording()
            self._start_playback(stream_index=stream_index,
                                 stream=stream)

            self.logger.info('Started recording pass-through from device ' +
                                '[{}] to sound device [{}]'.
                                format(input_device, output_device))

            recording_started_time = time.time()

            while self._get_recording_state() != RecordingState.STOPPED \
                    and (duration is None or
                            time.time() - recording_started_time < duration):
                while self._get_recording_state() == RecordingState.PAUSED:
                    self.recording_paused_changed.wait()

                time.sleep(0.1)

        except queue.Empty as e:
            self.logger.warning('Recording timeout: audio callback failed?')
        finally:
            self.stop_playback([stream_index])
            self.stop_recording()


    @action
    def query_streams(self):
        """
        :returns: A list of active audio streams
        """

        streams = {
            i: {
                attr: getattr(stream, attr)
                for attr in ['active', 'closed', 'stopped', 'blocksize',
                             'channels', 'cpu_load', 'device', 'dtype',
                             'latency', 'samplerate', 'samplesize']
                if hasattr(stream, attr)
            } for i, stream in self.active_streams.items()
        }

        for i, stream in streams.items():
            stream['playback_state'] = self.playback_state[i].name

        return streams


    def _allocate_stream_index(self, completed_callback_event=None):
        stream_index = None

        with self.playback_state_lock:
            for i in range(len(self.active_streams)+1):
                if i not in self.active_streams:
                    stream_index = i
                    break

            if stream_index is None:
                raise RuntimeError('No stream index available')

            self.active_streams[stream_index] = None
            self.completed_callback_events[stream_index] = \
                completed_callback_event if completed_callback_event else Event()

        return stream_index

    def _start_playback(self, stream_index, stream):
        with self.playback_state_lock:
            self.playback_state[stream_index] = PlaybackState.PLAYING
            self.active_streams[stream_index] = stream

            if isinstance(self.playback_paused_changed.get(stream_index), Event):
                self.playback_paused_changed[stream_index].clear()
            else:
                self.playback_paused_changed[stream_index] = Event()

        self.logger.info('Playback started on stream index {}'.
                         format(stream_index))

        return stream_index

    @action
    def stop_playback(self, streams=None):
        """
        :param streams: Streams to stop by index (default: all)
        :type streams: list[int]
        """

        with self.playback_state_lock:
            streams = streams or self.active_streams.keys()
            if not streams:
                return
            completed_callback_events = {}

            for i in streams:
                if i is None or not (i in self.active_streams):
                    continue

                stream = self.active_streams[i]
                if self.completed_callback_events[i]:
                    completed_callback_events[i] = self.completed_callback_events[i]
                self.playback_state[i] = PlaybackState.STOPPED

        for i, event in completed_callback_events.items():
            event.wait()
            del self.completed_callback_events[i]
            del self.active_streams[i]

        self.logger.info('Playback stopped on streams [{}]'.format(
            ', '.join([str(stream) for stream in
                       completed_callback_events.keys()])))

    @action
    def pause_playback(self, streams=None):
        """
        :param streams: Streams to pause by index (default: all)
        :type streams: list[int]
        """

        with self.playback_state_lock:
            streams = streams or self.active_streams.keys()
            if not streams:
                return

            for i in streams:
                if i is None or not (i in self.active_streams):
                    continue

                stream = self.active_streams[i]
                if self.playback_state[i] == PlaybackState.PAUSED:
                    self.playback_state[i] = PlaybackState.PLAYING
                elif self.playback_state[i] == PlaybackState.PLAYING:
                    self.playback_state[i] = PlaybackState.PAUSED
                else:
                    continue

                self.playback_paused_changed[i].set()

        self.logger.info('Playback pause toggled on streams [{}]'.format(
            ', '.join([str(stream) for stream in streams])))

    def start_recording(self):
        with self.recording_state_lock:
            self.recording_state = RecordingState.RECORDING

    @action
    def stop_recording(self):
        with self.recording_state_lock:
            self.recording_state = RecordingState.STOPPED
        self.logger.info('Recording stopped')

    @action
    def pause_recording(self):
        with self.recording_state_lock:
            if self.recording_state == RecordingState.PAUSED:
                self.recording_state = RecordingState.RECORDING
            elif self.recording_state == RecordingState.RECORDING:
                self.recording_state = RecordingState.PAUSED
            else:
                return

        self.logger.info('Recording paused state toggled')
        self.recording_paused_changed.set()

    def _get_playback_state(self, stream_index):
        with self.playback_state_lock:
            return self.playback_state[stream_index]

    def _get_recording_state(self):
        with self.recording_state_lock:
            return self.recording_state


# vim:sw=4:ts=4:et: