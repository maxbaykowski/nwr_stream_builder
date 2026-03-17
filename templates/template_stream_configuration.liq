#!/usr/bin/liquidsoap
stream = input.external.rawaudio(
  samplerate=16000,
  channels=1,
  buffer=2.0,
  max=20.0,
  restart=true,
  restart_on_error=true,
  "nc --recv-only --idle-timeout 5 --wait 5 127.0.0.1 7355 | csdr convert -ichar -ofloat | csdr shift 0 | csdr firdecimate 60 0.005 | csdr fmdemod | csdr deemphasis --wfm 16000 530e-6 | csdr gain 1.5 | csdr limit 1.0 | csdr convert -ifloat -os16"
  )
emergency = single("/usr/local/share/nwr/fallback.wav")
stream = filter(freq=3000.0, mode="low", q=1.0, stream)
radio = fallback([stream, emergency])
encoder = %mp3(bitrate=64, stereo=false, samplerate=16000)
rawaudio = %wav(header=false, stereo=false, samplesize=16, samplerate=16000)
output.external(
  rawaudio,
  "python3 /usr/local/bin/easrecorder.py --rate 16000 --max-seconds 300 --pre-seconds 10 --post-seconds 10 --outdir /var/lib/easrecorder/wxn99 --stdout --mp3 --local-time",
  radio)
output.icecast(
  encoder,
  host="localhost",
  port=8000,
  user="source",
  password="hackme",
  mount="/mount.mp3",
  name="<CALLSIGN123>",
  description="<CALLSIGN123> NOAA Weather Radio",
  genre="Weather",
  public=true,
  radio
  )
  
