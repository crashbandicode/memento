"use client";

import { createContext, useContext, useEffect, useState, ReactNode } from "react";
import { api, DeviceSummary } from "./api-client";

type Device = DeviceSummary;

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
    const saved = localStorage.getItem("dr_device_id");
    return saved && saved !== "all" ? saved : null;
  });

  useEffect(() => {
    // Only fetch if logged in
    const token = localStorage.getItem("dr_token");
    if (token) {
      api.getDevices()
        .then((nextDevices) => {
          setDevices(nextDevices);
          setSelectedDeviceId((current) => {
            if (current && !nextDevices.some((device) => device.device_id === current)) {
              localStorage.setItem("dr_device_id", "all");
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
    localStorage.setItem("dr_device_id", id || "all");
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
