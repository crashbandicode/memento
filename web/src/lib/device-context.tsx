"use client";

import { createContext, useContext, useEffect, useState, ReactNode } from "react";
import { api, DeviceGroupSummary } from "./api-client";
import { getStoredAuthToken } from "./auth-storage";

type Device = DeviceGroupSummary;

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
    const token = getStoredAuthToken();
    if (token) {
      api.getDeviceGroups()
        .then((nextDevices) => {
          setDevices(nextDevices);
          setSelectedDeviceId((current) => {
            if (!current) return null;
            if (nextDevices.some((device) => device.device_id === current)) {
              return current;
            }
            // Existing installs stored an individual collector ID. Migrate it
            // to the containing physical-host scope without losing the filter.
            const containingGroup = nextDevices.find((device) =>
              device.identities.some((identity) => identity.device_id === current)
            );
            if (containingGroup) {
              localStorage.setItem("dr_device_id", containingGroup.device_id);
              return containingGroup.device_id;
            }
            if (current) {
              localStorage.setItem("dr_device_id", "all");
              return null;
            }
            return null;
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
