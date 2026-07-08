// callguard — is the camera or microphone currently in use by ANY app?
//
// Reads CoreMediaIO's kCMIODevicePropertyDeviceIsRunningSomewhere (camera) and
// CoreAudio's kAudioDevicePropertyDeviceIsRunningSomewhere (mic). These read
// hardware *state* only — they never open a capture session, so the tool needs
// NO camera/mic (TCC) permission and never trips the privacy indicator.
//
// Purpose: let the voice-memo automation skip itself while Alex is on a video
// call (camera on) or any call/recording (mic on), so it never steals focus into
// the Voice Memos UI or hogs the Neural Engine mid-call.
//
// Output (stdout, exit 0):   camera=<0|1> mic=<0|1>
// Build: swiftc -O -framework CoreMediaIO -framework CoreAudio callguard.swift -o callguard

import CoreMediaIO
import CoreAudio

func cameraInUse() -> Bool {
    var addr = CMIOObjectPropertyAddress(
        mSelector: CMIOObjectPropertySelector(kCMIOHardwarePropertyDevices),
        mScope: CMIOObjectPropertyScope(kCMIOObjectPropertyScopeGlobal),
        mElement: CMIOObjectPropertyElement(kCMIOObjectPropertyElementMain))

    var dataSize: UInt32 = 0
    guard CMIOObjectGetPropertyDataSize(
        CMIOObjectID(kCMIOObjectSystemObject), &addr, 0, nil, &dataSize) == 0,
        dataSize > 0 else { return false }

    let count = Int(dataSize) / MemoryLayout<CMIOObjectID>.size
    var devices = [CMIOObjectID](repeating: 0, count: count)
    var used: UInt32 = 0
    guard CMIOObjectGetPropertyData(
        CMIOObjectID(kCMIOObjectSystemObject), &addr, 0, nil,
        dataSize, &used, &devices) == 0 else { return false }

    for dev in devices {
        var runAddr = CMIOObjectPropertyAddress(
            mSelector: CMIOObjectPropertySelector(kCMIODevicePropertyDeviceIsRunningSomewhere),
            mScope: CMIOObjectPropertyScope(kCMIOObjectPropertyScopeWildcard),
            mElement: CMIOObjectPropertyElement(kCMIOObjectPropertyElementWildcard))
        var running: UInt32 = 0
        var rUsed: UInt32 = 0
        let status = CMIOObjectGetPropertyData(
            dev, &runAddr, 0, nil,
            UInt32(MemoryLayout<UInt32>.size), &rUsed, &running)
        if status == 0 && running != 0 { return true }
    }
    return false
}

func micInUse() -> Bool {
    var addr = AudioObjectPropertyAddress(
        mSelector: kAudioHardwarePropertyDevices,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain)

    var dataSize: UInt32 = 0
    guard AudioObjectGetPropertyDataSize(
        AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, &dataSize) == 0,
        dataSize > 0 else { return false }

    let count = Int(dataSize) / MemoryLayout<AudioObjectID>.size
    var devices = [AudioObjectID](repeating: 0, count: count)
    guard AudioObjectGetPropertyData(
        AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil,
        &dataSize, &devices) == 0 else { return false }

    for dev in devices {
        // Only consider devices that actually have input streams (real mics).
        var streamAddr = AudioObjectPropertyAddress(
            mSelector: kAudioDevicePropertyStreams,
            mScope: kAudioObjectPropertyScopeInput,
            mElement: kAudioObjectPropertyElementMain)
        var streamSize: UInt32 = 0
        guard AudioObjectGetPropertyDataSize(dev, &streamAddr, 0, nil, &streamSize) == 0,
            streamSize > 0 else { continue }

        var runAddr = AudioObjectPropertyAddress(
            mSelector: kAudioDevicePropertyDeviceIsRunningSomewhere,
            mScope: kAudioObjectPropertyScopeInput,
            mElement: kAudioObjectPropertyElementMain)
        var running: UInt32 = 0
        var rSize = UInt32(MemoryLayout<UInt32>.size)
        let status = AudioObjectGetPropertyData(dev, &runAddr, 0, nil, &rSize, &running)
        if status == 0 && running != 0 { return true }
    }
    return false
}

let cam = cameraInUse()
let mic = micInUse()
print("camera=\(cam ? 1 : 0) mic=\(mic ? 1 : 0)")
