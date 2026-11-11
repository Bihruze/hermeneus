Synthetic fixtures written for the routing tests. Device names are generic
test names; no data from a user machine. The system_profiler files follow the
key layout of `system_profiler SPAudioDataType -json` on macOS. The transport
value "coreaudio_device_type_aggregate" is an assumption, because no
aggregate device existed on the machine the layout was read from.
coreaudio_multi_output.json mirrors CoreAudioDevice fields from
hermeneus_engine/tts/coreaudio_devices.py.

guard_lines.json holds synthetic lines written for the reply guard tests in
test_translator.py: English speech with Turkish replies that keep names, brands,
numbers, quotes and English terms, and the two leak shapes seen in real
sessions. No broadcast transcript text is stored here.
