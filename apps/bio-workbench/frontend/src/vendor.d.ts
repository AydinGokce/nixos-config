declare module "3dmol/build/3Dmol.es6.js" {
  export * from "3dmol";
}
interface Window {
  bioDesktop?: {
    platform: string;
    state?: {
      get(key: string): string | null;
      set(key: string, value: string): void;
    };
    openImportDialog?: () => Promise<void>;
    chooseSSHKey?: () => Promise<string | null>;
    onOpenBatch?: (callback: (id: string) => void) => () => void;
  };
}
