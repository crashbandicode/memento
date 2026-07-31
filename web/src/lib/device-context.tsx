"use client";

import { createContext, useContext, useEffect, useState, ReactNode } from "react";
import { api, DeviceSummary } from "./api-client";
import { getStoredAuthToken } from "./auth-storage";

type Device = DeviceSummary;

const DEVICE_SCOPE_STORAGE_KEY = "dr_device_scope_v2";
const LEGACY_DEVICE_SCOPE_STORAGE_KEY = "dr_device_id";

interface DeviceState {
  devices: Device[];
  selectedDeviceId: string | null; // null = all devices
  setSelectedDeviceId: (id: string | null) => void;
  deviceParam: string; // URL param string: "" or "&device_id=xxx"
}

const DeviceContext = createContext<DeviceState>({
  devices: [],
  selectedDeviceId: null,
  setSelectedDeviceId: () => {},
  deviceParam: "",
});

export function DeviceProvider({ children }: { children: ReactNode }) {
  const [devices, setDevices] = useState<Device[]>([]);
  // Lazy init from localStorage — avoids setState-in-effect rule.
  const [selectedDeviceId, setSelectedDeviceId] = useState<string | null>(() => {
    if (typeof window === "undefined") return null;
    const saved = localStorage.getItem(DEVICE_SCOPE_STORAGE_KEY);
    return saved && saved !== "all" ? saved : null;
  });

  useEffect(() => {
    // v1 device scope could silently disagree with the all-device dashboard.
    // Ignore it once so stale browser-specific selections (especially on
    // mobile) cannot hide conversations after this consistency fix.
    localStorage.removeItem(LEGACY_DEVICE_SCOPE_STORAGE_KEY);

    // Only fetch if logged in
    const token = getStoredAuthToken();
    if (token) {
      api.getDevices()
        .then((nextDevices) => {
          setDevices(nextDevices);
          setSelectedDeviceId((current) => {
            if (current && !nextDevices.some((device) => device.device_id === current)) {
              localStorage.setItem(DEVICE_SCOPE_STORAGE_KEY, "all");
              return null;
            }
            return current;
          });
        })
        .catch(() => {});
    }
  }, []);

  const handleSelect = (id: string | null) => {
    setSelectedDeviceId(id);
    localStorage.setItem(DEVICE_SCOPE_STORAGE_KEY, id || "all");
  };

  const deviceParam = selectedDeviceId ? `&device_id=${selectedDeviceId}` : "";

  return (
    <DeviceContext.Provider value={{ devices, selectedDeviceId, setSelectedDeviceId: handleSelect, deviceParam }}>
      {children}
    </DeviceContext.Provider>
  );
}

export function useDevice() {
  return useContext(DeviceContext);
}
