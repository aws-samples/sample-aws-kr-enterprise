export type ComponentType =
  | "TopAppBar"
  | "BottomNavigation"
  | "FloatingActionButton"
  | "Button"
  | "IconButton"
  | "TextField"
  | "OutlinedTextField"
  | "Card"
  | "ElevatedCard"
  | "OutlinedCard"
  | "ListItem"
  | "Checkbox"
  | "RadioButton"
  | "Switch"
  | "Slider"
  | "ProgressIndicator"
  | "CircularProgressIndicator"
  | "Dialog"
  | "AlertDialog"
  | "BottomSheet"
  | "Snackbar"
  | "Chip"
  | "FilterChip"
  | "Badge"
  | "Divider"
  | "Tab"
  | "TabRow"
  | "NavigationDrawer"
  | "DropdownMenu"
  | "Scaffold"
  | "Column"
  | "Row"
  | "LazyColumn"
  | "LazyRow"
  | "Box"
  | "Spacer"
  | "Surface"
  | "Image"
  | "Icon";

export interface DesignComponent {
  id: string;
  type: ComponentType;
  props: Record<string, unknown>;
  children?: DesignComponent[];
  style?: Record<string, string | number>;
}
